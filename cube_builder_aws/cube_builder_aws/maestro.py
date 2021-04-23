#
# This file is part of Python Module for Cube Builder AWS.
# Copyright (C) 2019-2021 INPE.
#
# Cube Builder AWS is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
#

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import numpy
import rasterio
from bdc_catalog.models import Band, Collection, GridRefSys, Item, Tile
from bdc_catalog.models.base_sql import db
from geoalchemy2 import func
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject, transform

from .constants import (APPLICATION_ID, CLEAR_OBSERVATION_ATTRIBUTES,
                        COG_MIME_TYPE, RESOLUTION_BY_SATELLITE, SRID_BDC_GRID)
from .logger import logger
from .utils.processing import (create_asset_definition, create_cog_in_s3,
                               create_index, encode_key, format_version,
                               generateQLook, get_cube_name, getMask,
                               qa_statistics)
from .utils.timeline import Timeline


def orchestrate(cube_irregular_infos, temporal_schema, tiles, start_date, end_date, shape=None, item_prefix=None):
    formatted_version = format_version(cube_irregular_infos.version)

    tiles_by_grs = db.session() \
        .query(Tile, GridRefSys) \
        .filter(
            Tile.grid_ref_sys_id == cube_irregular_infos.grid_ref_sys_id,
            Tile.name.in_(tiles),
            GridRefSys.id == Tile.grid_ref_sys_id
        ).all()

    tiles_infos = []
    for tile in tiles_by_grs:
        grid_table = tile.GridRefSys.geom_table
        
        tile_stats = db.session.query(
            (func.ST_XMin(grid_table.c.geom)).label('min_x'),
            (func.ST_YMax(grid_table.c.geom)).label('max_y'),
            (func.ST_XMax(grid_table.c.geom) - func.ST_XMin(grid_table.c.geom)).label('dist_x'),
            (func.ST_YMax(grid_table.c.geom) - func.ST_YMin(grid_table.c.geom)).label('dist_y'),
            (func.ST_AsGeoJSON(func.ST_Transform(grid_table.c.geom, 4326))).label('feature')
        ).filter(
            grid_table.c.tile == tile.Tile.name
        ).first()

        tiles_infos.append(dict(
            id=tile.Tile.id,
            name=tile.Tile.name,
            stats=tile_stats
        ))

    # get cube start_date if exists
    start_date = datetime.strptime(start_date, '%Y-%m-%d').date()

    end_date = datetime.strptime(end_date, '%Y-%m-%d').date()

    # Get/Mount timeline from given parameters
    timeline = Timeline(**temporal_schema, start_date=start_date, end_date=end_date).mount()

    # create collection items (old model => mosaic)
    items_id = []
    prefix = '' if item_prefix is None else str(item_prefix)
    items = {}
    for interval in sorted(timeline):
        
        interval_start = interval[0]
        interval_end = interval[1]

        if start_date is not None and interval_start < start_date : continue
        if end_date is not None and interval_end > end_date : continue

        for tile in tiles_infos:
            tile_id = tile['id']
            tile_name = tile['name']
            tile_stats = tile['stats']
            feature = tile_stats.feature

            items[tile_name] = items.get(tile_name, {})
            items[tile_name]['geom'] = feature
            items[tile_name]['xmin'] = tile_stats.min_x
            items[tile_name]['ymax'] = tile_stats.max_y
            items[tile_name]['dist_x'] = tile_stats.dist_x
            items[tile_name]['dist_y'] = tile_stats.dist_y
            items[tile_name]['periods'] = items[tile_name].get('periods', {})

            period = f'{interval_start}_{interval_end}'

            item_id = f'{cube_irregular_infos.name}_{formatted_version}_{tile_name}_{period}'
            if item_id not in items_id:
                items_id.append(item_id)
                items[tile_name]['periods'][period] = {
                    'tile_id': tile_id,
                    'tile_name': tile_name,
                    'item_date': period,
                    'id': item_id,
                    'composite_start': interval_start,
                    'composite_end': interval_end,
                    'dirname': f'{os.path.join(prefix, cube_irregular_infos.name, formatted_version, tile_name)}/'
                }
                if shape:
                    items[tile_name]['periods'][period]['shape'] = shape

    return items


def get_key_to_controltable(activity):
    activitiesControlTableKey = activity['dynamoKey']
    
    if activity['action'] != 'publish':
        activitiesControlTableKey = activitiesControlTableKey.replace(activity['band'], '')

    if activity['action'] == 'merge':
        activitiesControlTableKey = activitiesControlTableKey.replace(
            activity['date'], '{}{}'.format(activity['start'], activity['end']))

    return activitiesControlTableKey


def solo(self, activitylist):
    services = self.services

    for activity in activitylist:
        services.put_activity(activity)

        activitiesControlTableKey = get_key_to_controltable(activity)

        if activity['mystatus'] == 'DONE':
            next_step(services, activity)

        elif activity['mystatus'] == 'ERROR':
            response = services.update_control_table(
                Key = {'id': activitiesControlTableKey},
                UpdateExpression = "ADD #errors :increment",
                ExpressionAttributeNames = {'#errors': 'errors'},
                ExpressionAttributeValues = {':increment': 1},
                ReturnValues = "UPDATED_NEW"
            )


def next_step(services, activity):
    activitiesControlTableKey = get_key_to_controltable(activity)

    response = services.update_control_table(
        Key = {'id': activitiesControlTableKey},
        UpdateExpression = "SET #mycount = #mycount + :increment, #end_date = :date",
        ExpressionAttributeNames = {'#mycount': 'mycount', '#end_date': 'end_date'},
        ExpressionAttributeValues = {':increment': 1, ':date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
        ReturnValues = "UPDATED_NEW"
    )
    if 'Attributes' in response and 'mycount' in response['Attributes']:
        mycount = int(response['Attributes']['mycount'])

        if mycount >= activity['totalInstancesToBeDone']:
            if activity['action'] == 'merge':
                next_blend(services, activity)

            elif activity['action'] == 'blend':
                if activity.get('bands_expressions') and len(activity['bands_expressions'].keys()) > 0:
                    next_posblend(services, activity)
                else:
                    next_publish(services, activity)

            elif activity['action'] == 'posblend':
                next_publish(services, activity)


###############################
# MERGE
###############################
def prepare_merge(self, datacube, irregular_datacube, datasets, satellite, bands, bands_ids, 
                  quicklook, resx, resy, nodata, crs, quality_band, functions, version,
                  force=False, mask=None, bands_expressions=dict(), indexes_only_regular_cube=False):
    services = self.services

    # Build the basics of the merge activity
    activity = {}
    activity['action'] = 'merge'
    activity['datacube'] = datacube
    activity['irregular_datacube'] = irregular_datacube
    activity['version'] = version
    activity['datasets'] = datasets
    activity['satellite'] = satellite.upper()
    activity['bands'] = bands
    activity['bands_ids'] = bands_ids
    activity['bands_expressions'] = bands_expressions
    activity['quicklook'] = quicklook
    activity['resx'] = resx
    activity['resy'] = resy
    activity['nodata'] = nodata
    activity['srs'] = crs
    activity['bucket_name'] = services.bucket_name
    activity['quality_band'] = quality_band
    activity['functions'] = functions
    activity['force'] = force
    activity['indexes_only_regular_cube'] = indexes_only_regular_cube
    activity['internal_bands'] = ['CLEAROB', 'TOTALOB', 'PROVENANCE']

    activity['stac_list'] = []
    for stac in services.stac_list:
        stac_cp = deepcopy(stac)
        del stac_cp['instance']
        activity['stac_list'].append(stac_cp)

    logger.info('prepare merge - Score {} items'.format(self.score['items']))

    scenes_not_started = []

    # For all tiles
    for tile_name in self.score['items']:
        activity['tileid'] = tile_name
        activity['tileid_original'] = tile_name
        activity['mask'] = mask

        # GET bounding box - tile ID
        activity['geom'] = self.score['items'][tile_name]['geom']
        activity['xmin'] = self.score['items'][tile_name]['xmin']
        activity['ymax'] = self.score['items'][tile_name]['ymax']
        activity['dist_x'] = self.score['items'][tile_name]['dist_x']
        activity['dist_y'] = self.score['items'][tile_name]['dist_y']

        # For all periods
        for periodkey in self.score['items'][tile_name]['periods']:
            activity['start'] = self.score['items'][tile_name]['periods'][periodkey]['composite_start']
            activity['end'] = self.score['items'][tile_name]['periods'][periodkey]['composite_end']
            activity['dirname'] = self.score['items'][tile_name]['periods'][periodkey]['dirname']
            activity['shape'] = self.score['items'][tile_name]['periods'][periodkey].get('shape')

            # convert to string
            activity['start'] = activity['start'].strftime('%Y-%m-%d')
            activity['end'] = activity['end'].strftime('%Y-%m-%d')

            # When force is True, we must rebuild the merge
            publish_control_key = 'publish{}{}'.format(activity['datacube'], encode_key(activity, ['tileid', 'start', 'end']))
            if not force:
                response = services.get_activity_item({'id': publish_control_key, 'sk': 'ALLBANDS' })
                if 'Item' in response and response['Item']['mystatus'] == 'DONE':
                    scenes_not_started.append(f'{activity["tileid"]}_{activity["start"]}_{activity["end"]}')
                    continue
            else:
                merge_control_key = encode_key(activity, ['action', 'irregular_datacube', 'tileid', 'start', 'end'])
                blend_control_key = 'blend{}{}'.format(activity['datacube'], encode_key(activity, ['tileid', 'start', 'end']))
                posblend_control_key = 'posblend{}{}'.format(activity['datacube'], encode_key(activity, ['tileid', 'start', 'end']))
                self.services.remove_control_by_key(merge_control_key)
                self.services.remove_control_by_key(blend_control_key)
                self.services.remove_control_by_key(posblend_control_key)
                self.services.remove_control_by_key(publish_control_key)

            self.score['items'][tile_name]['periods'][periodkey]['scenes'] = services.search_STAC(activity)

            # Evaluate the number of dates, the number of scenes for each date and
            # the total amount merges that will be done
            number_of_datasets_dates = 0
            if len(self.score['items'][tile_name]['periods'][periodkey]['scenes'].keys()):
                first_band = list(self.score['items'][tile_name]['periods'][periodkey]['scenes'].keys())[0]
                activity['list_dates'] = []
                for dataset in self.score['items'][tile_name]['periods'][periodkey]['scenes'][first_band].keys():
                    for date in self.score['items'][tile_name]['periods'][periodkey]['scenes'][first_band][dataset].keys():
                        activity['list_dates'].append(date)
                        number_of_datasets_dates += 1
            
            activity['instancesToBeDone'] = number_of_datasets_dates
            activity['totalInstancesToBeDone'] = number_of_datasets_dates * len(activity['bands'])

            # Reset mycount in activitiesControlTable
            activities_control_table_key = encode_key(activity, ['action','irregular_datacube','tileid','start','end'])
            services.put_control_table(activities_control_table_key, 0, activity['totalInstancesToBeDone'], 
                                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

            # Save the activity in DynamoDB if no scenes are available
            if number_of_datasets_dates == 0:
                dynamo_key = encode_key(activity, ['action','irregular_datacube','tileid','start','end'])
                activity['dynamoKey'] = dynamo_key
                activity['sk'] = 'NOSCENES'
                activity['mystatus'] = 'ERROR'
                activity['mylaunch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                activity['mystart'] = 'XXXX-XX-XX'
                activity['myend'] = 'YYYY-YY-YY'
                activity['efficacy'] = '0'
                activity['cloudratio'] = '100'
                services.put_item_kinesis(activity)
                continue

            # Build each merge activity
            # For all bands
            for band in self.score['items'][tile_name]['periods'][periodkey]['scenes']:
                activity['band'] = band

                # For all datasets
                for dataset in self.score['items'][tile_name]['periods'][periodkey]['scenes'][band]:
                    activity['dataset'] = dataset

                    # For all dates
                    for date in self.score['items'][tile_name]['periods'][periodkey]['scenes'][band][dataset]:
                        activity['date'] = date[0:10]
                        activity['links'] = []

                        # Create the dynamoKey for the activity in DynamoDB
                        activity['dynamoKey'] = encode_key(activity, ['action','irregular_datacube','tileid','date','band'])

                        # Get all scenes that were acquired in the same date
                        for scene in self.score['items'][tile_name]['periods'][periodkey]['scenes'][band][dataset][date]:
                            activity['links'].append(scene['link'])
                            if 'source_nodata' in scene:
                                activity['source_nodata'] = scene['source_nodata']

                        # Continue filling the activity
                        activity['ARDfile'] = activity['dirname']+'{}/{}_{}_{}_{}_{}.tif'.format(activity['date'],
                            activity['irregular_datacube'], version, activity['tileid'], activity['date'], band)
                        activity['sk'] = activity['date']
                        activity['mylaunch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                        # Check if we have already done and no need to do it again
                        response = services.get_activity_item({'id': activity['dynamoKey'], 'sk': activity['sk'] })
                        if 'Item' in response:
                            if not force and response['Item']['mystatus'] == 'DONE' \
                                    and services.s3_file_exists(key=activity['ARDfile']):
                                next_step(services, activity)
                                continue
                            else:
                                services.remove_activity_by_key(activity['dynamoKey'], activity['sk'])

                        # Re-schedule a merge-warped
                        activity['mystatus'] = 'NOTDONE'
                        activity['mystart'] = 'SSSS-SS-SS'
                        activity['myend'] = 'EEEE-EE-EE'
                        activity['efficacy'] = '0'
                        activity['cloudratio'] = '100'

                        # Send to queue to activate merge lambda
                        services.put_item_kinesis(activity)
                        services.send_to_sqs(activity)

    return scenes_not_started

def merge_warped(self, activity):
    logger.info('==> start MERGE')
    services = self.services
    key = activity['ARDfile']
    bucket_name = activity['bucket_name']
    prefix = services.get_s3_prefix(bucket_name)

    mystart = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    activity['mystart'] = mystart
    activity['mystatus'] = 'DONE'
    satellite = activity.get('satellite')
    activity_mask = activity['mask']

    # Flag to force merge generation without cache
    force = activity.get('force')

    try:
        # If ARDfile already exists, update activitiesTable and chech if this merge is the last one for the mosaic
        if not force and services.s3_file_exists(bucket_name=bucket_name, key=key):
            efficacy = 0
            cloudratio = 100
            try:
                with rasterio.open('{}{}'.format(prefix, key)) as src:
                    values = src.read(1)
                    if activity['band'] == activity['quality_band']:
                        efficacy, cloudratio = qa_statistics(values, mask=activity_mask, blocks=list(src.block_windows()))

                # Update entry in DynamoDB
                activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                activity['efficacy'] = str(efficacy)
                activity['cloudratio'] = str(cloudratio)
                services.put_item_kinesis(activity)

                return
            except:
                _ = services.delete_file_S3(bucket_name=bucket_name, key=key)

        # Lets warp and merge
        resx = float(activity['resx'])
        resy = float(activity['resy'])
        xmin = float(activity['xmin'])
        ymax = float(activity['ymax'])
        dist_x = float(activity['dist_x'])
        dist_y = float(activity['dist_y'])
        nodata = int(activity['nodata'])
        band = activity['band']

        shape = activity.get('shape', None)
        if shape:
            num_pixel_x = shape[0]
            num_pixel_y = shape[1]

        else:
            num_pixel_x = round(dist_x / resx)
            num_pixel_y = round(dist_y / resy)

            new_res_x = dist_x / num_pixel_x
            new_res_y = dist_y / num_pixel_y

            transform = Affine(new_res_x, 0, xmin, 0, -new_res_y, ymax)

        numcol = num_pixel_x
        numlin = num_pixel_y

        is_sentinel_landsat_quality_fmask = ('LANDSAT' in satellite or satellite == 'SENTINEL-2') and \
                                            (band == activity['quality_band'] and activity_mask['nodata'] != 0)
        source_nodata = 0

        # Quality band is resampled by nearest, other are bilinear
        if band == activity['quality_band']:
            resampling = Resampling.nearest

            nodata = activity_mask['nodata']
            source_nodata = nodata

            raster = numpy.zeros((numlin, numcol,), dtype=numpy.uint16)
            raster_merge = numpy.full((numlin, numcol,), dtype=numpy.uint16, fill_value=source_nodata)
            raster_mask = numpy.ones((numlin, numcol,), dtype=numpy.uint16)
        
        else:
            resampling = Resampling.bilinear
            raster = numpy.zeros((numlin, numcol,), dtype=numpy.int16)
            raster_merge = numpy.full((numlin, numcol,), fill_value=nodata, dtype=numpy.int16)

        # For all files
        template = None
        raster_blocks = None

        for url in activity['links']:

            with rasterio.Env(CPL_CURL_VERBOSE=False):
                with rasterio.open(url) as src:

                    kwargs = src.meta.copy()
                    kwargs.update({
                        'width': numcol,
                        'height': numlin
                    })
                    if not shape:
                        kwargs.update({
                            'crs': activity['srs'],
                            'transform': transform
                        })

                    if src.profile['nodata'] is not None:
                        source_nodata = src.profile['nodata']
                    
                    elif 'source_nodata' in activity:
                        source_nodata = activity['source_nodata']

                    elif 'LANDSAT' in satellite and band != activity['quality_band']:
                        source_nodata = nodata if src.profile['dtype'] == 'int16' else 0

                    elif 'CBERS' in satellite and band != activity['quality_band']:
                        source_nodata = nodata

                    kwargs.update({
                        'nodata': source_nodata
                    })

                    with MemoryFile() as memfile:
                        with memfile.open(**kwargs) as dst:
                            if shape:
                                raster = src.read(1)
                            else:
                                reproject(
                                    source=rasterio.band(src, 1),
                                    destination=raster,
                                    src_transform=src.transform,
                                    src_crs=src.crs,
                                    dst_transform=transform,
                                    dst_crs=activity['srs'],
                                    src_nodata=source_nodata,
                                    dst_nodata=nodata,
                                    resampling=resampling)

                            if band != activity['quality_band'] or is_sentinel_landsat_quality_fmask:
                                valid_data_scene = raster[raster != nodata]
                                raster_merge[raster != nodata] = valid_data_scene.reshape(numpy.size(valid_data_scene))

                                valid_data_scene = None
                            else:
                                factor = raster * raster_mask

                                raster_merge = raster_merge + factor
                                raster_mask[raster != nodata] = 0

                            if template is None:
                                template = dst.profile

                                template['driver'] = 'GTiff'

                                raster_blocks = list(dst.block_windows())

                                if band != activity['quality_band']:
                                    template.update({'dtype': 'int16'})
                                    template['nodata'] = nodata

        raster = None
        raster_mask = None

        # Evaluate cloud cover and efficacy if band is quality
        efficacy = 0
        cloudratio = 100
        if activity['band'] == activity['quality_band']:
            raster_merge, efficacy, cloudratio = getMask(raster_merge, mask=activity_mask, blocks=raster_blocks)
            template.update({'dtype': 'uint8'})
            nodata = activity_mask['nodata']

            # Save merged image on S3
            create_cog_in_s3(services, template, key, raster_merge, True, nodata, bucket_name)
        else:
            create_cog_in_s3(services, template, key, raster_merge, False, None, bucket_name)

        # Update entry in DynamoDB
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        activity['efficacy'] = str(efficacy)
        activity['cloudratio'] = str(cloudratio)
        activity['new_resolution_x'] = str(new_res_x)
        activity['new_resolution_y'] = str(new_res_y)
        services.put_item_kinesis(activity)

    except Exception as e:
        # Update entry in DynamoDB
        activity['mystatus'] = 'ERROR'
        activity['errors'] = dict(
            step='merge',
            message=str(e)
        )
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        services.put_item_kinesis(activity)

        logger.error(str(e), exc_info=True)


###############################
# BLEND
###############################
def next_blend(services, mergeactivity):
    # Fill the blend activity from merge activity
    blendactivity = {}
    blendactivity['action'] = 'blend'
    for key in ['datasets', 'satellite', 'bands', 'quicklook', 'srs', 'functions', 'bands_ids',
                'tileid', 'start', 'end', 'dirname', 'nodata', 'bucket_name', 'quality_band',
                'internal_bands', 'force', 'version', 'datacube', 'irregular_datacube', 'mask',
                'bands_expressions', 'indexes_only_regular_cube']:
        blendactivity[key] = mergeactivity.get(key, '')

    blendactivity['totalInstancesToBeDone'] = len(blendactivity['bands']) + len(blendactivity['internal_bands'])

    # Create  dynamoKey for the blendactivity record
    blendactivity['dynamoKey'] = encode_key(blendactivity, ['action','datacube','tileid','start','end'])

    # Fill the blendactivity fields with data for quality band from the DynamoDB merge records
    blendactivity['scenes'] = {}
    mergeactivity['band'] = mergeactivity['quality_band']
    blendactivity['band'] = mergeactivity['quality_band']
    status = fill_blend(services, mergeactivity, blendactivity)

    # Reset mycount in  activitiesControlTable
    activitiesControlTableKey = blendactivity['dynamoKey']
    services.put_control_table(activitiesControlTableKey, 0, blendactivity['totalInstancesToBeDone'],
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    # If no quality file was found for this tile/period, register it in DynamoDB and go on
    if not status or blendactivity['instancesToBeDone'] == 0:
        blendactivity['sk'] = 'ALLBANDS'
        blendactivity['mystatus'] = 'ERROR'
        blendactivity['errors'] = dict(
            step='next_blend',
            message='not all merges were found for this tile/period'
        )
        blendactivity['mystart'] = 'SSSS-SS-SS'
        blendactivity['myend'] = 'EEEE-EE-EE'
        blendactivity['efficacy'] = '0'
        blendactivity['cloudratio'] = '100'
        blendactivity['mylaunch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        services.put_item_kinesis(blendactivity)
        return False

    # Fill the blendactivity fields with data for the other bands from the DynamoDB merge records
    for band in (blendactivity['bands'] + blendactivity['internal_bands']):
        # if internal process, duplique first band and set the process in activity
        internal_band = band if band in blendactivity['internal_bands'] else False
        if internal_band:
            band = blendactivity['bands'][0]

        mergeactivity['band'] = band
        blendactivity['band'] = band
        _ = fill_blend(services, mergeactivity, blendactivity, internal_band)
        
        blendactivity['sk'] = internal_band if internal_band else band

        # Check if we are doing it again and if we have to do it because a different number of ARDfiles is present
        response = services.get_activity_item(
            {'id': blendactivity['dynamoKey'], 'sk': blendactivity['sk'] })

        if 'Item' in response \
                and response['Item']['mystatus'] == 'DONE' \
                and response['Item']['instancesToBeDone'] == blendactivity['instancesToBeDone']:

            exists = True
            for func in blendactivity['functions']:
                if func == 'IDT' or (func == 'MED' and internal_band == 'PROVENANCE'): continue
                if not services.s3_file_exists(bucket_name=mergeactivity['bucket_name'], key=blendactivity['{}file'.format(func)]):
                    exists = False

            if not blendactivity.get('force') and exists:
                next_step(services, response['Item'])
                continue
            else:
                services.remove_activity_by_key(blendactivity['dynamoKey'], blendactivity['sk'])

        # Blend has not been performed, do it
        blendactivity['mystatus'] = 'NOTDONE'
        blendactivity['mystart'] = 'SSSS-SS-SS'
        blendactivity['myend'] = 'EEEE-EE-EE'
        blendactivity['efficacy'] = '0'
        blendactivity['cloudratio'] = '100'
        blendactivity['mylaunch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        services.put_item_kinesis(blendactivity)
        services.send_to_sqs(blendactivity)

        # Leave room for next band in blendactivity
        for date_ref in blendactivity['scenes']:
            if band in blendactivity['scenes'][date_ref]['ARDfiles'] \
                and band != mergeactivity['quality_band']:
                del blendactivity['scenes'][date_ref]['ARDfiles'][band]
    return True

def fill_blend(services, mergeactivity, blendactivity, internal_band=False):
    # Fill blend activity fields with data for band from the DynamoDB merge records
    band = blendactivity['band']
    blendactivity['instancesToBeDone'] = 0

    cube_version = blendactivity['version']

    # Query dynamoDB to get all merged
    items = []
    for date in mergeactivity['list_dates']:
        mergeactivity['date_formated'] = date

        # verify if internal process
        if internal_band:
            blendactivity['internal_band'] = internal_band
        dynamoKey = encode_key(mergeactivity, ['action','irregular_datacube','tileid','date_formated','band'])
        
        response = services.get_activities_by_key(dynamoKey)
        if 'Items' not in response or len(response['Items']) == 0:
            return False
        items += response['Items']

    for item in items:
        if item['mystatus'] != 'DONE':
            return False
        activity = json.loads(item['activity'])
        date_ref = item['sk']
        if date_ref not in blendactivity['scenes']:
            blendactivity['scenes'][date_ref] = {}
            blendactivity['scenes'][date_ref]['efficacy'] = item['efficacy']
            blendactivity['scenes'][date_ref]['date'] = activity['date']
            blendactivity['scenes'][date_ref]['dataset'] = activity['dataset']
            blendactivity['scenes'][date_ref]['satellite'] = activity['satellite']
            blendactivity['scenes'][date_ref]['cloudratio'] = item['cloudratio']
        if 'ARDfiles' not in blendactivity['scenes'][date_ref]:
            blendactivity['scenes'][date_ref]['ARDfiles'] = {}
        basename = os.path.basename(activity['ARDfile'])
        blendactivity['scenes'][date_ref]['ARDfiles'][band] = basename

    blendactivity['instancesToBeDone'] += len(items)
    if band != blendactivity['quality_band']:
        for function in blendactivity['functions']:
            if func == 'IDT': continue
            cube_id = '{}_{}'.format('_'.join(blendactivity['datacube'].split('_')[:-1]), function)
            blendactivity['{}file'.format(function)] = '{0}/{5}/{1}/{2}_{3}/{0}_{5}_{1}_{2}_{3}_{4}.tif'.format(
                cube_id, blendactivity['tileid'], blendactivity['start'], blendactivity['end'], band, cube_version)
    else:
        # quality band generate only STK composite
        cube_id = '{}_{}'.format('_'.join(blendactivity['datacube'].split('_')[:-1]), 'STK')
        blendactivity['{}file'.format('STK')] = '{0}/{5}/{1}/{2}_{3}/{0}_{5}_{1}_{2}_{3}_{4}.tif'.format(
            cube_id, blendactivity['tileid'], blendactivity['start'], blendactivity['end'], band, cube_version)
    return True

def blend(self, activity):
    logger.info('==> start BLEND')
    services = self.services

    activity['mystart'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    activity['sk'] = activity['band'] if not activity.get('internal_band') else activity['internal_band']
    bucket_name = activity['bucket_name']
    prefix = services.get_s3_prefix(bucket_name)
    activity_mask = activity['mask']

    band = activity['band']
    numscenes = len(activity['scenes'])

    nodata = int(activity.get('nodata', -9999))
    if band == activity['quality_band']:
        nodata = activity_mask['nodata']

    # Check if band ARDfiles are in activity
    for date_ref in activity['scenes']:
        if band not in activity['scenes'][date_ref]['ARDfiles']:
            activity['mystatus'] = 'ERROR'
            activity['errors'] = dict(
                step='blend',
                message='ERROR band {}'.format(band)
            )
            services.put_item_kinesis(activity)
            return

    try:
        # Get basic information (profile) of input files
        keys = list(activity['scenes'].keys())
        filename = os.path.join(
            prefix + activity['dirname'],
            activity['scenes'][keys[0]]['date'],
            activity['scenes'][keys[0]]['ARDfiles'][band])

        with rasterio.open(filename) as src:
            profile = src.profile
            tilelist = list(src.block_windows())

        # Order scenes based in efficacy/resolution
        mask_tuples = []
        for key in activity['scenes']:
            scene = activity['scenes'][key]
            efficacy = float(scene['efficacy'])
            resolution = 10
            mask_tuples.append((100. * efficacy / resolution, key))

        # Open all input files and save the datasets in two lists, one for masks and other for the current band.
        # The list will be ordered by efficacy/resolution
        masklist = []
        bandlist = []
        dates = []
        for m in sorted(mask_tuples, reverse=True):
            key = m[1]
            dates.append(key[:10])
            efficacy = m[0]
            scene = activity['scenes'][key]

            # MASK -> Quality
            filename = os.path.join(
                prefix + activity['dirname'],
                scene['date'],
                scene['ARDfiles'][activity['quality_band']])
            quality_ref = rasterio.open(filename)

            try:
                masklist.append(rasterio.open(filename))
            except:
                activity['mystatus'] = 'ERROR'
                activity['errors'] = dict(
                    step= 'blend',
                    message='ERROR {}'.format(os.path.basename(filename))
                )
                services.put_item_kinesis(activity)
                return

            # BANDS
            filename = os.path.join(
                prefix + activity['dirname'],
                scene['date'],
                scene['ARDfiles'][band])
            try:
                bandlist.append(rasterio.open(filename))
            except:
                activity['mystatus'] = 'ERROR'
                activity['errors'] = dict(
                    step= 'blend',
                    message='ERROR {}'.format(os.path.basename(filename))
                )
                services.put_item_kinesis(activity)
                return

        # Build the raster to store the output images.
        width = profile['width']
        height = profile['height']

        clear_values = numpy.array(activity_mask['clear_data'])
        not_clear_values = numpy.array(activity_mask['not_clear_data'])
        saturated_values = numpy.array(activity_mask['saturated_data'])

        # STACK and MED will be generated in memory
        stack_raster = numpy.full((height, width), dtype=profile['dtype'], fill_value=nodata)
        if 'MED' in activity['functions']:
            median_raster = numpy.full((height, width), dtype=profile['dtype'], fill_value=nodata)

        # Build the stack total observation
        build_total_observation = activity.get('internal_band') == 'TOTALOB'
        if build_total_observation:
            stack_total_observation = numpy.zeros((height, width), dtype=numpy.uint8)

        # create file to save clear observation, total oberservation and provenance
        build_clear_observation = activity.get('internal_band') == 'CLEAROB'
        if build_clear_observation:
            clear_ob_file = '/tmp/clearob.tif'
            clear_ob_profile = profile.copy()
            clear_ob_profile['dtype'] = CLEAR_OBSERVATION_ATTRIBUTES['data_type']
            clear_ob_profile.pop('nodata', None)
            clear_ob_dataset = rasterio.open(clear_ob_file, mode='w', **clear_ob_profile)

        # Build the stack total observation
        build_provenance = activity.get('internal_band') == 'PROVENANCE'
        if build_provenance:
            provenance_array = numpy.full((height, width), dtype=numpy.int16, fill_value=-1)

        for _, window in tilelist:
            # Build the stack to store all images as a masked array. At this stage the array will contain the masked data
            stackMA = numpy.ma.zeros((numscenes, window.height, window.width), dtype=numpy.int16)

            notdonemask = numpy.ones(shape=(window.height, window.width), dtype=numpy.bool_)

            row_offset = window.row_off + window.height
            col_offset = window.col_off + window.width

            # For all pair (quality,band) scenes
            for order in range(numscenes):
                # Read both chunk of Merge and Quality, respectively.
                ssrc = bandlist[order]
                msrc = masklist[order]
                raster = ssrc.read(1, window=window)
                mask = msrc.read(1, window=window)

                if build_total_observation:
                    copy_mask = numpy.array(mask, copy=True)

                # Mask cloud/snow/shadow/no-data as False
                mask[numpy.where(numpy.isin(mask, not_clear_values))] = 0
                # Saturated values as False
                mask[numpy.where(numpy.isin(mask, saturated_values))] = 0
                # Ensure that Raster nodata value (-9999 maybe) is set to False
                mask[raster == nodata] = 0
                # Mask valid data (0 and 1) as True
                mask[numpy.where(numpy.isin(mask, clear_values))] = 1

                # Create an inverse mask value in order to pass to numpy masked array
                # True => nodata
                bmask = numpy.invert(mask.astype(numpy.bool_))

                # Use the mask to mark the fill (0) and cloudy (2) pixels
                stackMA[order] = numpy.ma.masked_where(bmask, raster)

                if build_total_observation:
                    # Copy Masked values in order to stack total observation
                    copy_mask[raster != nodata] = 1
                    copy_mask[raster == nodata] = 0

                    stack_total_observation[window.row_off: row_offset, window.col_off: col_offset] += copy_mask.astype(numpy.uint8)

                # Get current observation file name
                if build_provenance:
                    day_of_year = datetime.strptime(dates[order], '%Y-%m-%d').timetuple().tm_yday

                # Find all no data in destination STACK image
                stack_raster_where_nodata = numpy.where(
                    stack_raster[window.row_off: row_offset, window.col_off: col_offset] == nodata
                )

                # Turns into a 1-dimension
                stack_raster_nodata_pos = numpy.ravel_multi_index(
                    stack_raster_where_nodata,
                    stack_raster[window.row_off: row_offset, window.col_off: col_offset].shape
                )

                # Find all valid/cloud in destination STACK image
                raster_where_data = numpy.where(raster != nodata)
                raster_data_pos = numpy.ravel_multi_index(raster_where_data, raster.shape)

                # Match stack nodata values with observation
                # stack_raster_where_nodata && raster_where_data
                intersect_ravel = numpy.intersect1d(stack_raster_nodata_pos, raster_data_pos)

                if len(intersect_ravel):
                    where_intersec = numpy.unravel_index(intersect_ravel, raster.shape)
                    stack_raster[window.row_off: row_offset, window.col_off: col_offset][where_intersec] = raster[where_intersec]

                    if build_provenance:
                        provenance_array[window.row_off: row_offset, window.col_off: col_offset][where_intersec] = day_of_year

                # Identify what is needed to stack, based in Array 2d bool
                todomask = notdonemask * numpy.invert(bmask)

                # Find all positions where valid data matches.
                clear_not_done_pixels = numpy.where(numpy.logical_and(todomask, mask.astype(numpy.bool)))

                # Override the STACK Raster with valid data.
                stack_raster[window.row_off: row_offset, window.col_off: col_offset][clear_not_done_pixels] = raster[
                    clear_not_done_pixels]

                if build_provenance:
                    # Mark day of year to the valid pixels
                    provenance_array[window.row_off: row_offset, window.col_off: col_offset][
                        clear_not_done_pixels] = day_of_year

                # Update what was done.
                notdonemask = notdonemask * bmask

            if 'MED' in activity['functions']:
                median = numpy.ma.median(stackMA, axis=0).data
                median[notdonemask.astype(numpy.bool_)] = nodata
                median_raster[window.row_off: row_offset, window.col_off: col_offset] = median.astype(profile['dtype'])

            if build_clear_observation:
                count_raster = numpy.ma.count(stackMA, axis=0)

                clear_ob_dataset.write(count_raster.astype(clear_ob_profile['dtype']), window=window, indexes=1)

        # Close all input dataset
        for order in range(numscenes):
            bandlist[order].close()
            masklist[order].close()

        # Evaluate cloud cover
        if activity['quality_band'] == band:
            efficacy, cloud_cover = qa_statistics(stack_raster, mask=activity_mask, blocks=tilelist)

            activity['efficacy'] = str(efficacy)
            activity['cloudratio'] = str(cloud_cover)

        # Upload the CLEAROB dataset
        if build_clear_observation:
            clear_ob_dataset.close()
            clear_ob_dataset = None
            for func in activity['functions']:
                if func == 'IDT': continue
                key_clearob = '_'.join(activity['{}file'.format(func)].split('_')[:-1]) + '_CLEAROB.tif'
                services.upload_file_S3(clear_ob_file, key_clearob, {'ACL': 'public-read'}, bucket_name=bucket_name)
            os.remove(clear_ob_file)

        # Upload the PROVENANCE dataset
        if build_provenance and 'STK' in activity['functions']:
            provenance_profile = profile.copy()
            provenance_profile.pop('nodata',  -1)
            provenance_profile['dtype'] = 'int16'
            provenance_key = '_'.join(activity['STKfile'].split('_')[:-1]) + '_PROVENANCE.tif'
            create_cog_in_s3(
                services, provenance_profile, provenance_key, provenance_array, 
                False, None, bucket_name)

        # Upload the TOTALOB dataset
        if build_total_observation:
            total_observation_profile = profile.copy()
            total_observation_profile.pop('nodata', None)
            total_observation_profile['dtype'] = 'uint8'
            for func in activity['functions']:
                if func == 'IDT': continue
                total_ob_key = '_'.join(activity['{}file'.format(func)].split('_')[:-1]) + '_TOTALOB.tif'
                create_cog_in_s3(
                    services, total_observation_profile, total_ob_key, stack_total_observation, 
                    False, None, bucket_name)

        # Create and upload the STACK dataset
        if 'STK' in activity['functions']:
            if not activity.get('internal_band'):
                create_cog_in_s3(
                    services, profile, activity['STKfile'], stack_raster, 
                    (band != activity['quality_band']), nodata, bucket_name)

        stack_raster = None

        # Create and upload the STACK dataset
        if 'MED' in activity['functions']:
            if not activity.get('internal_band'):
                create_cog_in_s3(
                    services, profile, activity['MEDfile'], median_raster, 
                    (band != activity['quality_band']), nodata, bucket_name)

        # Update status and end time in DynamoDB
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        activity['mystatus'] = 'DONE'
        services.put_item_kinesis(activity)

    except Exception as e:
        # Update entry in DynamoDB
        activity['mystatus'] = 'ERROR'
        activity['errors'] = dict(
            step='blend',
            message=str(e)
        )
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        services.put_item_kinesis(activity)

        logger.error(str(e), exc_info=True)


###############################
# POS BLEND
###############################
def next_posblend(services, blendactivity):
    # Fill the blend activity from merge activity
    blend_dynamo_key = blendactivity['dynamoKey']
    posblendactivity = blendactivity
    posblendactivity['action'] = 'posblend'

    indexes_only_regular_cube = posblendactivity.get('indexes_only_regular_cube', False)

    # indexes * (Irregular scenes + regular scene)
    quantity_scenes = 1 if indexes_only_regular_cube else (len(posblendactivity['scenes'].keys()) + 1)
    posblendactivity['totalInstancesToBeDone'] = len(posblendactivity['bands_expressions'].keys()) * quantity_scenes

    # Reset mycount in activitiesControlTable
    if posblendactivity['action'] not in posblendactivity['dynamoKey']:
        posblendactivity['dynamoKey'] = blend_dynamo_key.replace('blend', posblendactivity['action'])
    activitiesControlTableKey = posblendactivity['dynamoKey']
    services.put_control_table(activitiesControlTableKey, 0, posblendactivity['totalInstancesToBeDone'],
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    posblendactivity['indexesToBe'] = {}
    for i_name in posblendactivity['bands_expressions'].keys():
        i_infos = posblendactivity['bands_expressions'][i_name]

        posblendactivity['indexesToBe'][i_name] = {}

        for band_id in i_infos['expression']['bands']:
            band_name = posblendactivity['bands_ids'][str(band_id)]

            # get Blend activity
            response = services.get_activity_item({'id': blend_dynamo_key, 'sk': band_name})
            item = response['Item']
            activity = json.loads(item['activity'])

            for func in posblendactivity['functions']:
                posblendactivity['indexesToBe'][i_name][func] = posblendactivity['indexesToBe'][i_name].get(func, {})

                if func == 'IDT':
                    if indexes_only_regular_cube:
                        continue

                    dates = activity['scenes'].keys()
                    for date_ref in dates:
                        scene = activity['scenes'][date_ref]

                        date = scene['date']
                        posblendactivity['indexesToBe'][i_name][func][date] = posblendactivity['indexesToBe'][i_name][func].get(date, {})
                        path_band = '{}{}/{}'.format(activity['dirname'], date, scene['ARDfiles'][band_name])
                        posblendactivity['indexesToBe'][i_name][func][date][band_name] = path_band

                else:
                    posblendactivity['indexesToBe'][i_name][func][band_name] = activity['{}file'.format(func)]

    for i_name in posblendactivity['bands_expressions'].keys():
        # create and dispatch one activity to irregular cube and one to regular cubes (each index)

        functions = [''] if indexes_only_regular_cube else ['', 'IDT']
        for i in functions:
            posblendactivity['sk'] = '{}{}'.format(i_name, i)

            # Blend has not been performed, do it
            posblendactivity['mystatus'] = 'NOTDONE'
            posblendactivity['mystart'] = 'SSSS-SS-SS'
            posblendactivity['myend'] = 'EEEE-EE-EE'
            posblendactivity['efficacy'] = '0'
            posblendactivity['cloudratio'] = '100'
            posblendactivity['mylaunch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            response = services.get_activity_item({'id': posblendactivity['dynamoKey'], 'sk': posblendactivity['sk']})
            if 'Item' in response \
                and response['Item']['mystatus'] == 'DONE' \
                and response['Item']['instancesToBeDone'] == blendactivity['instancesToBeDone']:

                if not posblendactivity.get('force'):
                    next_step(services, response['Item'])
                    continue
                else:
                    services.remove_activity_by_key(posblendactivity['dynamoKey'], posblendactivity['sk'])

            if i == 'IDT':
                dates_refs = activity['scenes'].keys()
                for date_ref in dates_refs:
                    scene = activity['scenes'][date_ref]
                    date = scene['date']
                    posblendactivity['sk'] = '{}{}{}'.format(i_name, i, date)
            
                    services.put_item_kinesis(posblendactivity)
                    services.send_to_sqs(posblendactivity)
            else:
                services.put_item_kinesis(posblendactivity)
                services.send_to_sqs(posblendactivity)

    return True

def posblend(self, activity):
    logger.info('==> start POS BLEND')
    services = self.services

    force = activity.get('force', False)
    indexes_only_regular_cube = activity.get('indexes_only_regular_cube', False)

    bucket_name = activity['bucket_name']
    mystart = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    reprocessed = False
    bands_expressions = activity['bands_expressions']

    try:
        sk = activity['sk']
        is_identity = 'IDT' in activity['sk']
        
        if is_identity and not indexes_only_regular_cube:
            sk = sk.replace('IDT', '')

            index_name = sk[:-10]
            index = activity['indexesToBe'][sk[:-10]]

            for date in index['IDT'].keys():
                if date in sk:
                    bands = index['IDT'][date]

                    # get first band link to mount others paths
                    first_band = list(bands.keys())[0]
                    path_first_band = bands[first_band]

                    i_file_path = path_first_band.replace(f'_{first_band}.tif', f'_{index_name}.tif') 
                    if force or not services.s3_file_exists(bucket_name=bucket_name, key=i_file_path):
                        create_index(services, index_name, bands_expressions, bands, bucket_name, i_file_path)
                        reprocessed = True
        else:
            index_name = sk
            index = activity['indexesToBe'][index_name]

            for func in activity['functions']:
                if func == 'IDT': continue
                bands = index[func]

                # get first band link to mount others paths
                first_band = list(bands.keys())[0]
                path_first_band = bands[first_band]

                i_file_path = path_first_band.replace(f'_{first_band}.tif', f'_{index_name}.tif')
                if force or not services.s3_file_exists(bucket_name=bucket_name, key=i_file_path):
                    create_index(services, index_name, bands_expressions, bands, bucket_name, i_file_path)
                    reprocessed = True
                
        # Update status and end time in DynamoDB
        if reprocessed:
            activity['mystart'] = mystart
            activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        activity['mystatus'] = 'DONE'
        services.put_item_kinesis(activity)
    
    except Exception as e:
        # Update entry in DynamoDB
        activity['mystatus'] = 'ERROR'
        activity['errors'] = dict(
            step='posblend',
            message=str(e)
        )
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        services.put_item_kinesis(activity)

        logger.error(str(e), exc_info=True)


###############################
# PUBLISH
###############################
def next_publish(services, posblendactivity):
    # Fill the publish activity from blend activity
    publishactivity = {}
    for key in ['datacube','bands','bands_ids','quicklook','tileid','start','end', \
        'dirname', 'cloudratio', 'bucket_name', 'quality_band', 'internal_bands', \
        'functions', 'indexesToBe', 'version', 'irregular_datacube', \
        'indexes_only_regular_cube', 'force']:
        publishactivity[key] = posblendactivity.get(key)
    publishactivity['action'] = 'publish'

    indexes_only_regular_cube = posblendactivity.get('indexes_only_regular_cube', False)

    # Create dynamoKey for the publish activity
    publishactivity['dynamoKey'] = encode_key(publishactivity, ['action','datacube','tileid','start','end'])

    # Get information from blended bands
    response = services.get_activities_by_key(posblendactivity['dynamoKey'].replace('posblend', 'blend'))

    items = response['Items']
    publishactivity['scenes'] = {}
    publishactivity['blended'] = {}
    example_file_name = ''
    for item in items:
        activity = json.loads(item['activity'])
        band = item['sk']
        if band not in publishactivity['bands']: continue

        # Get ARD files
        for date_ref in activity['scenes']:
            scene = activity['scenes'][date_ref]
            if date_ref not in publishactivity['scenes']:
                publishactivity['scenes'][date_ref] = {'ARDfiles' : {}}
                publishactivity['scenes'][date_ref]['ARDfiles'][publishactivity['quality_band']] = scene['ARDfiles'][publishactivity['quality_band']]
                publishactivity['scenes'][date_ref]['date'] = scene['date']
                publishactivity['scenes'][date_ref]['cloudratio'] = scene['cloudratio']

                # add indexes to publish in irregular cube
                if not indexes_only_regular_cube:
                    for index_name in publishactivity['indexesToBe'].keys():
                        # ex: LC8_30_090096_2019-01-28_fMask.tif => LC8_30_090096_2019-01-28_NDVI.tif
                        quality_file = scene['ARDfiles'][publishactivity['quality_band']]
                        index_file_name = quality_file.replace(f'_{publishactivity["quality_band"]}.tif', f'_{index_name}.tif')
                        publishactivity['scenes'][date_ref]['ARDfiles'][index_name] = index_file_name

            publishactivity['scenes'][date_ref]['ARDfiles'][band] = scene['ARDfiles'][band]

        # Get blended files
        publishactivity['blended'][band] = {}
        for func in publishactivity['functions']:
            if func == 'IDT': continue
            if func == 'MED' and band == publishactivity['quality_band']: continue

            key_file = '{}file'.format(func)
            publishactivity['blended'][band][key_file] = activity[key_file]
            example_file_name = activity[key_file]

    # Create indices to catalog CLEAROB, TOTALOB, PROVENANCE, ...
    for internal_band in publishactivity['internal_bands']:
        publishactivity['blended'][internal_band] = {}

        for func in publishactivity['functions']:
            if func == 'IDT': continue
            if func == 'MED' and internal_band == 'PROVENANCE': continue

            key_file = '{}file'.format(func)
            file_name = '_'.join(example_file_name.split('_')[:-1]) + '_{}.tif'.format(internal_band)
            publishactivity['blended'][internal_band][key_file] = file_name

    # Create indices to catalog EVI, NDVI ...
    for index_name in publishactivity['indexesToBe'].keys():
        publishactivity['blended'][index_name] = {}

        for func in publishactivity['functions']:
            if func == 'IDT': continue

            key_file = '{}file'.format(func)
            file_name = '_'.join(example_file_name.split('_')[:-1]) + '_{}.tif'.format(index_name)
            publishactivity['blended'][index_name][key_file] = file_name

    publishactivity['sk'] = 'ALLBANDS'
    publishactivity['mystatus'] = 'NOTDONE'
    publishactivity['mylaunch'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    publishactivity['mystart'] = 'SSSS-SS-SS'
    publishactivity['myend'] = 'EEEE-EE-EE'
    publishactivity['efficacy'] = '0'
    publishactivity['cloudratio'] = '100'
    publishactivity['instancesToBeDone'] = len(publishactivity['bands']) - 1
    publishactivity['totalInstancesToBeDone'] = 1

    # Reset mycount in activitiesControlTable
    activitiesControlTableKey = publishactivity['dynamoKey']
    services.put_control_table(activitiesControlTableKey, 0, publishactivity['totalInstancesToBeDone'],
                                datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    response = services.get_activity_item({'id': publishactivity['dynamoKey'], 'sk': 'ALLBANDS'})
    if 'Item' in response and response['Item']['mystatus'] == 'DONE':
        if not publishactivity.get('force'):
            next_step(services, response['Item'])
            return
        else:
            services.remove_activity_by_key(publishactivity['dynamoKey'], 'ALLBANDS')

    # Launch activity
    services.put_item_kinesis(publishactivity)
    services.send_to_sqs(publishactivity)

def publish(self, activity):
    logger.info('==> start PUBLISH')
    services = self.services
    bucket_name = activity['bucket_name']
    prefix = services.get_s3_prefix(bucket_name)

    indexes_only_regular_cube = activity.get('indexes_only_regular_cube', False)

    activity['mystart'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        identity_cube = activity['irregular_datacube']

        # GENERATE QUICKLOOK's and REGISTER ITEMS in DB
        ## for CUBES (MEDIAN, STACK ...)
        qlbands = activity['quicklook']
        for function in activity['functions']:
            if function == 'IDT': continue

            cube_name = activity['datacube']
            cube = Collection.query().filter(
                Collection.name == cube_name,
                Collection.version == int(activity['version'][-3:])
            ).first()
            if not cube:
                raise Exception(f'cube {cube_name} - {activity["version"]} not found!')

            general_scene_id = '{}_{}_{}_{}_{}'.format(
                cube_name, activity['version'], activity['tileid'], activity['start'], activity['end'])

            # Generate quicklook
            qlfiles = []
            for band in qlbands:
                qlfiles.append(prefix + activity['blended'][band][function + 'file'])
    
            png_name = generateQLook(general_scene_id, qlfiles)
            png_file_name = Path(png_name).name
            if png_name is None:
                raise Exception(f'publish - Error generateQLook for {general_scene_id}')

            dirname_ql = activity['dirname'].replace(f'{identity_cube}/', f'{cube_name}/')
            range_date = f'{activity["start"]}_{activity["end"]}'
            s3_pngname = os.path.join(dirname_ql, range_date, png_file_name)
            services.upload_file_S3(png_name, s3_pngname, {'ACL': 'public-read'}, bucket_name=bucket_name)
            os.remove(png_name)

            quicklook_url = f'{bucket_name}/{s3_pngname}'

            # register items in DB
            with db.session.begin_nested():
                item = Item.query().filter(
                    Item.name == general_scene_id,
                    Item.collection_id == cube.id).first()
                if not item:
                    tile = Tile.query().filter(
                        Tile.name == activity['tileid'],
                        Tile.grid_ref_sys_id == cube.grid_ref_sys_id
                    ).first()
                    
                    item = Item(
                        name=general_scene_id,
                        collection_id=cube.id,
                        tile_id=tile.id,
                        start_date=activity['start'],
                        end_date=activity['end'],
                        cloud_cover=float(activity['cloudratio']),
                        srid=SRID_BDC_GRID,
                        application_id=APPLICATION_ID
                    )

                thumbnail, _, _ = create_asset_definition(
                    services, bucket_name, str(s3_pngname), 'image/png', ['thumbnail'], quicklook_url)
                assets = dict(thumbnail=thumbnail)

                # add 'assets'
                bands_by_cube = Band.query().filter(
                    Band.collection_id == cube.id
                ).all()

                indexes_list = list(activity['indexesToBe'].keys())
                for band in (activity['bands'] + activity['internal_bands'] + indexes_list):
                    if not activity['blended'][band].get('{}file'.format(function)):
                        continue

                    band_model = next(filter(lambda b: str(b.name) == band, bands_by_cube), None)
                    if not band_model:
                        raise Exception(f'band {band} not found!')

                    relative_path = activity["blended"][band][function + "file"]        
                    full_path = f'{bucket_name}/{relative_path}'
                    assets[band_model.name], item.geom, item.min_convex_hull = create_asset_definition(
                        services, bucket_name,
                        relative_path, COG_MIME_TYPE, ['data'],
                        full_path, is_raster=True
                    )

                item.assets = assets
                item.updated = datetime.now()
                db.session.add(item)
            db.session.commit()

        ## for all ARD scenes (IDENTITY)
        for date_ref in activity['scenes']:
            scene = activity['scenes'][date_ref]

            cube_name = activity['irregular_datacube']
            cube = Collection.query().filter(
                Collection.name == cube_name,
                Collection.version == int(activity['version'][-3:])
            ).first()
            if not cube:
                raise Exception(f'cube {cube_name} - {activity["version"]} not found!')

            general_scene_id = '{}_{}_{}_{}'.format(
                cube_name, activity['version'], activity['tileid'], str(scene['date'])[0:10])

            # Generate quicklook
            qlfiles = []
            for band in qlbands:
                filename = os.path.join(prefix + activity['dirname'], str(scene['date'])[0:10], scene['ARDfiles'][band])
                qlfiles.append(filename)

            png_name = generateQLook(general_scene_id, qlfiles)
            png_file_name = Path(png_name).name
            if png_name is None:
                raise Exception(f'publish - Error generateQLook for {general_scene_id}')

            date = str(scene['date'])[0:10]
            s3_pngname = os.path.join(activity['dirname'], date, png_file_name)
            services.upload_file_S3(png_name, s3_pngname, {'ACL': 'public-read'}, bucket_name=bucket_name)
            os.remove(png_name)

            quicklook_url = f'{bucket_name}/{s3_pngname}'

            # register items in DB
            with db.session.begin_nested():
                item = Item.query().filter(
                    Item.name == general_scene_id,
                    Item.collection_id == cube.id).first()
                if not item:
                    tile = Tile.query().filter(
                        Tile.name == activity['tileid'],
                        Tile.grid_ref_sys_id == cube.grid_ref_sys_id
                    ).first()
                    
                    item = Item(
                        name=general_scene_id,
                        collection_id=cube.id,
                        tile_id=tile.id,
                        start_date=scene['date'],
                        end_date=scene['date'],
                        cloud_cover=float(scene['cloudratio']),
                        srid=SRID_BDC_GRID,
                        application_id=APPLICATION_ID
                    )

                thumbnail, _, _ = create_asset_definition(
                    services, bucket_name, str(s3_pngname), 'image/png', ['thumbnail'], quicklook_url)
                assets = dict(thumbnail=thumbnail)

                # insert 'assets'
                bands_by_cube = Band.query().filter(
                    Band.collection_id == cube.id
                ).all()
                
                indexes_list = [] if indexes_only_regular_cube else list(activity['indexesToBe'].keys())
                for band in (activity['bands'] + indexes_list):
                    if band not in scene['ARDfiles']:
                        raise Exception(f'publish - problem - band {band} not in scene[files]')

                    band_model = next(filter(lambda b: str(b.name) == band, bands_by_cube), None)
                    if not band_model:
                        raise Exception(f'band {band} not found!')
                    
                    relative_path = os.path.join(activity['dirname'], str(scene['date'])[0:10], scene['ARDfiles'][band])
                    full_path = f'{bucket_name}/{relative_path}'
                    assets[band_model.name], item.geom, item.min_convex_hull = create_asset_definition(
                        services, bucket_name,
                        relative_path, COG_MIME_TYPE, ['data'],
                        full_path, is_raster=True
                    )
                
                item.assets = assets
                item.updated = datetime.now()
                db.session.add(item)
            db.session.commit()

        # Update status and end time in DynamoDB
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        activity['mystatus'] = 'DONE'
        services.put_item_kinesis(activity)

    except Exception as e:
        activity['mystatus'] = 'ERROR'
        activity['errors'] = dict(
            step='publish',
            message=str(e)
        )
        activity['myend'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        services.put_item_kinesis(activity)

        logger.error(str(e), exc_info=True)