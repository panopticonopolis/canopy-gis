import ee
import yaml
import time
import os
import json
import time
import pandas as pd
from pandas.tseries.offsets import DateOffset
from argparse import ArgumentParser
from utils import clipToROI, exportImageCollectionToGCS, exportImageToGCS, sentinel2CloudScore, calcCloudCoverage, inject_B10, sentinel2ProjectShadows, computeQualityScore, mergeCollection
from utils import GEETaskManager
from utils import collection_greater_than

from gevent.fileobject import FileObjectThread
import logging


LOG_FILENAME = 'full_basin_export_history.log'
logging.basicConfig(filename=LOG_FILENAME,level=logging.INFO)


def makeImageCollection(ee_dir, ee_roi, start_date, end_date, filters_before=None, modifiers=[], filters_after=None):
    '''
    Queries for Google Earth Engine (GEE) images based off of the input values. See below for details.
    Outputs a GEE ImageCollection object.

    The following arguments are required--

    "ee_dir": The GEE directory you want to pull images from. For example, to get Sentinel-2 L2A images,
              input "COPERNICUS/S2_SR".

    "ee_roi": A GEE Geometry Polygon object specifying the bounds of the image query.

    "start_date": A string containing the first date of the date range you want your images to be from;
                  e.g., "2020-10-30".

    "end_date": A string containing the last date of the date range you want your images to be from.

    The following arguments are optional--

    "filters_before": A list of ee.Filter objects you want to apply to the ImageCollection *before* the modifiers
                      are applied. Default is None.
    
    "modifiers": A list of functions to map onto the ImageCollection using GEE's ".map()" method. Each function
                 must have as its input and output only a single GEE Image object. All functions will be applied
                 to all the Images in the ImageCollection. Defaults to an empty list.

    "filters_after": A list of ee.Filter objects you want to apply to the ImageCollection *after* the modifiers
                     are applied. Default is None.
    '''
    ## Split into the original call/filters and the mapping
    
    #print(modifiers)

    collection = ee.ImageCollection(ee_dir) \
                .filterDate(ee.Date(start_date), ee.Date(end_date)) \
                .filterBounds(ee_roi) \
                .map( lambda x: clipToROI(x, ee.Geometry(ee_roi)) )

    #print("size of collection:",collection.size().getInfo())

    if filters_before is not None:
        collection = collection.filter( filters_before )
    
    for m in modifiers:
        #print(f'Applying modifier {m}')
        collection = collection.map(m)

    if filters_after:
        collection = collection.filter( filters_after )

    return collection


class Pipeline:
    '''

    Pipeline object for exporting Cloudfree Merging images using Google Earth Engine (GEE).
    Requires a configuration file in yml format (see ___ for details). Other arguments are optional.

    Arguments:
    "config_file": The location of your configuration file.

    "polygons": You may choose to enter a GEE FeatureCollection object here that contains the Regions of Interest (ROI's)
    you wish to export. If so, please also input a date_range_list.
    If you don't use this argument, make sure to run the "import_aois" method. Default value is None.

    "date_range_list": If you enter polygons, you must also enter a date range list.
    The date range list should be a list of dictionaries. There should be one dictionary per polygon in your "polygons" FeatureCollection.
    If you don't use this argument, make sure to run the "import_aois" method. Default value is None.
    Each dictionary must contain at least the following two keys:
        "start_date": The first date to query images from for that polygon. This date should be a string in 'year-month-day' format.
            Examples: '2020-01-01'; '2019-11-30'
        "end_date": The last date to query images from for that polygon. This date should be a string in 'year-month-day' format.
            Examples: '2020-01-01'; '2019-11-30'
    If you use Dynamic Date Range, each dictionary must also include the following two key/value pairs:
        "original_date": The 'central' date of your date range. This date should be a string in 'year-month-day' format.
            Examples: '2020-01-01'; '2019-11-30'
        "day_offset": To get your first date range while using Dynamic Date Range, take the original_date and expand forward and back
        by the number of days indicated by day_offset. For example, if the original_date is July 1 2020, and your day_offset is 30,
        then your start_date will be June 1 2020, and your end_date will be July 31 2020. This should be an integer.
        When using Dynamic Date Range, we highly recommend either using the "import_aois" method, or deciding on your original_date
        and day_offset first and generating the start_date and end_date from them.

    "dynamic_date_range": Boolean for whether to use the Dynamic Date Range feature. Default is False.

    "offset_array": If 'dynamic_date_range' is set to True and 'polygons' are provided, then an offset_array must also be provided.
    This should be a list of integers. The smallest integer should be the smallest. day_offset used in the date_range_list. If an
    insufficient amount of images are found using that offset, the pipeline will then use the second-smallest integer as the new offset,
    and so on. Once the highest integer is reached, that will be used as the final date range.
    Defaults to None.

    "debug": Boolean for whether to log each export and the time of each export. Defaults to False.

    "qual_img_thresh": Integer that defines the highest percent cloudcover an image may have to be considered a "quality image." Defaults to 5.

    "qual_img_amoount": Integer that defines how many quality images must be found for the given date range to be considered fine. In other words,
    if the amount of quality images found are below this amount, the date range expands and images are queried again. Defaults to 5.

    '''

    def __init__(self, config_file, polygons=None, dynamic_date_range=False,
                 date_range_list=None, offset_array=None, debug=False, 
                 qual_img_thresh=5, qual_img_amount=5):
        self.load_config(config_file)
        self.polygons = polygons
        self.dynamic_date_range = dynamic_date_range
        self.date_range_list = date_range_list
        self.debug = debug

        self._minutes_to_wait = 60
        self.exports = []
        self.exceptions = []
        self.qual_img_thresh = qual_img_thresh
        self.qual_img_amount = qual_img_amount  
        
        
        if self.polygons and self.date_range_list is None:
            raise ValueError('If you input polygons, you must also input a date_range_list')
        if self.date_range_list:
            self._check_date_range_list()
        if self.dynamic_date_range:
            if offset_array:
                self.offset_dict = self._create_offset_dict(offset_array)
            else:
                if self.polygons:
                    raise ValueError('With dynamic date range, if you input polygons you must also input an offset_array')

        self._initialize_ee()


    def _initialize_ee(self):
        '''
        Helper function that initializes GEE
        '''

        try:
            ee.Initialize()
        except:
            ee.Authenticate()
            ee.Initialize() 


    def _check_date_range_list(self):
        '''
        Helper function that checks to make sure the date range list is in the proper format and raises an error if it isn't
        '''

        t = type(self.date_range_list)
        if t is not list:
            raise ValueError(f'date_range_list is a {t}, but it must be a list')

        polygon_type = type(self.polygons)
        if self.polygons is None:
            raise ValueError('If you input a date_range_list, you must also input polygons')
        elif type(polygon_type) is ee.featurecollection.FeatureCollection:
            feature_list = self.polygons.toList(self.polygons.size())
            n_polygons = feature_list.size().getInfo()
        else:
            raise ValueError(f'Polygons are a {polygon_type}, but they must be an ee.FeatureCollection')

        if len(self.date_range_list) != n_polygons:
            raise ValueError('date_range_list and polygons must have the same length')

        offset_value = None
        for d in self.date_range_list:
            if type(d) is not dict:
                raise ValueError(f'date_range_list contains a {type(d)}, but it must solely contain dictionaries')

            if self.dynamic_date_range:
                required_keys = ['start_date', 'end_date', 'original_date', 'day_offset']
            else:
                required_keys = ['start_date', 'end_date']
            if len(d.keys()) != len(required_keys):
                if self.dynamic_date_range:
                    raise ValueError(f'With dynamic date range, all dicts must have {len(required_keys)} keys, but at least one has {len(d.keys())} keys')
                else:
                    raise ValueError(f'Without dynamic date range, all dicts must have {len(required_keys)} keys, but at least one has {len(d.keys())} keys')

            for key in d.keys():
                if key not in required_keys:
                    raise KeyError(f'At least one dict has an inappropriate key: {key}')
                elif (self.dynamic_date_range is True) and (key == 'day_offset'):
                    if offset_value is None:
                        offset_value = d[key]
                    else:
                        if d[key] != offset_value:
                            raise ValueError('Every date range in the date_range_list must have the same day_offset')


    def _create_offset_dict(self, offset_array):
        '''
        Helper function that creates an offset dictionary based on an input array

        "offset_array": List containing offset values
        '''

        if type(offset_array) is not list:
            raise ValueError(f'offset_array is a {type(offset_array)}, but it must be a list')

        offset_array.sort()
        off_arr_copy = offset_array.copy() 
        d = {}
        for i, value in enumerate(off_arr_copy):
            if i < len(off_arr_copy) - 1:
                d[value] = off_arr_copy[i+1]

        return d


    def _create_filename_parts(self):
        '''
        Helper function that creates a filename based on the configuration file
        '''

        if isinstance(self.source['name'], str):
            self.source['name'] = [self.source['name']]

        if 'prefix' in self.sensor:
            if isinstance(self.sensor['prefix'], str):
                self.sensor['prefix'] = [self.sensor['prefix']]
            filename_parts = self.sensor['prefix'] + self.source['name']
        else:
            filename_parts = self.source['name']

        return filename_parts


    def _get_feature_area(self, feature):
        '''
        Helper function that gets the area of a GEE Feature object

        "feature": GEE Feature object
        '''

        stateArea = feature.geometry().area()
        stateAreaSqKm = ee.Number(stateArea).divide(1e6).round()

        return stateAreaSqKm.getInfo()

    def _makeFilterList(self):
        '''Helper function that builds a filter list based off of the configuration file'''
        filters_before = None
        filters_after = None

        def _build_filters(filter_list):
            filters = []
            for f in filter_list:
                key = list(f.keys())[0]
                op = list(list(f.values())[0].keys())[0]
                val = list(list(f.values())[0].values())[0]
                filters.append(getattr(ee.Filter, op)(key, val))

            return filters

        if 'filters_before' in self.sensor:
            filters_before = _build_filters(self.sensor['filters_before'])

        if 'filters_after' in self.sensor:
            filters_after = _build_filters(self.sensor['filters_after'])

        return filters_before, filters_after


    def load_config(self, config_file):
        '''
        Loads a configuration file

        "config_file": The location of the configuration file
        '''

        stream = open(config_file, 'r') 
        config_dict = yaml.load(stream)
        self.source = config_dict['data_list'][0]
        self.sensor = config_dict['sensors'][0]
        self.export_folder = config_dict['bucket']
        return config_dict


    def import_aois(self, csv_loc, Full_Congo_Pull=False, start_date=None,
                    end_date=None, days_duration=90, poly_start=0, poly_limit=None):
        '''

        '''
        features = []
        polygons = []
        day_offset = days_duration / 2
        start_end_list = []
        
        if Full_Congo_Pull:
            with open(csv_loc,"r",encoding='utf-8') as jsonfile:
                data = json.load(jsonfile)
                for feature_id, geometry in enumerate(data["features"], 1):
                    polygon = geometry["geometry"]["coordinates"][0][0]
                    poly_obj = ee.Geometry.Polygon(polygon)
                    feature = ee.Feature(poly_obj, {"name": feature_id})
                    features.append(feature)
                    
                original_date = pd.to_datetime('01-01-2020')
                start = (original_date + DateOffset(days=-30))
                end = (original_date + DateOffset(days=30))
                start_date = str(start)[:10]
                end_date = str(end)[:10]
                date_dict = {
                    'start_date': start_date,
                    'end_date': end_date,
                    'original_date': original_date,
                    'day_offset': 30,
                }
                
                start_end_list = [date_dict] * len(features)
                fc = ee.FeatureCollection(features)

                offset_dict = {
                    30: 360
                }
                
                self.polygons = fc
                self.date_range_list = start_end_list
                self.offset_dict = offset_dict
                return fc, start_end_list, offset_dict
                
        else:
            feature_id = poly_start
            
            if poly_limit:
                df_labels = pd.read_csv(csv_loc, skiprows=range(1, poly_start+1), nrows=poly_limit)
            else:
                df_labels = pd.read_csv(csv_loc, skiprows=range(1, poly_start+1))

            df_labels = df_labels[["center-lat","center-long","polygon","Labels combined","tile date","area (km2)"]]
            df_labels["tile date"] = pd.to_datetime(df_labels["tile date"])
            start = (df_labels["tile date"] + DateOffset(days=-day_offset))
            end = (df_labels["tile date"] + DateOffset(days=day_offset))
            for i in range(len(start)):
                start_date = str(start[i])[:10]
                end_date = str(end[i])[:10]
                original_date = df_labels.loc[i, 'tile date']
                date_dict = {
                    'start_date': start_date,
                    'end_date': end_date,
                    'original_date': original_date,
                    'day_offset': day_offset,
                }
                start_end_list.append(date_dict)

            for polygon in df_labels["polygon"]:
                polygons.append(json.loads(polygon)["coordinates"])
                
            for poly in polygons:
                # create an roi. first item in Misha's label list
                feature_id += 1 
                # create geometry object, create feature object, append to features list for feature collection creation 
                polys = ee.Geometry.Polygon(poly)
                feature = ee.Feature(polys,{"name":feature_id})
                features.append(feature)

            fc = ee.FeatureCollection(features)

            offset_dict = {
                45: 90,
                90: 180,
                180: 360
            }
                
            self.polygons = fc
            self.date_range_list = start_end_list
            self.offset_dict = offset_dict
            return fc, start_end_list, offset_dict


    def process_datasource_custom_daterange(
        self, loop_start=0, limit=None, minutes_to_wait=60, polygon_id_list=None
    ):
        '''
        Applies Cloudfree Merging to the polygons in the GEE FeatureCollection and exports them one at a time.

        IMPORTANT NOTE: GEE only allows 3,000 exports in your export queue at any one time. Please make sure
        your feature collection does not exceed 3,000.

        "loop_start": This method loops through your entire FeatureCollection by default. If you wish to instead
        start the loop at a specific value, input that value here. Default is 0.

        "limit": Again, this method loops through the entire FeatureCollection by default.
        If you want to only export the first X polygons instead, input that X here.

        "minutes_to_wait": If GEE times out, this method waits 60 minutes by default before starting again where it left off.
        You may change the number of minutes it waits here.

        "polygon_id_list": If you only want to export certain polygons, enter their IDs into a list and input that list here.
        '''
        # source = self.source
        # sensor = self.sensor
        # export_folder = self.export_folder
        # polygons = self.polygons
        # date_range_list = self.date_range_list
        # debug = self.debug

        feature_list = self.polygons.sort(self.source['sort_by']).toList(self.polygons.size())
        n_polygons = feature_list.size().getInfo()
        print(f"{n_polygons} features have been loaded")

        #task_list = []

        exports = []
        exceptions = []

        filename_parts = self._create_filename_parts()

        if not limit:
            limit = n_polygons

        ### ERROR? ###
        ## Originally this was range(1, n_features), but we're pretty sure
        ## that should be 0 so we changed it.


        if polygon_id_list:
            if type(polygon_id_list) is list or type(polygon_id_list) is tuple:
                if all(isinstance(x, int) for x in polygon_id_list) is True:
                    loop_values = [poly_id - 1 for poly_id in polygon_id_list]
                else:
                    raise ValueError(f'all values within polygon_id_list should be of type {int}')    
            else:
                raise ValueError('polygon_id_list has to be either a list or a tuple')
                        
            
        else:
            loop_values = range(loop_start, limit)

        for i in loop_values:
            polygon_id = i + 1

            print(f'Processing polygon {polygon_id} of {limit}', end='\r', flush=True)

            feature = ee.Feature( feature_list.get(i) )
            if self._get_feature_area(feature) > 1000:
                print(f'Polygon {polygon_id} has area greater than 1000; skipping')
            else:
                roi = feature.geometry()
                roi = roi.coordinates().getInfo()[0]
                # tile = None

                time_stamp = "_".join(time.ctime().split(" ")[1:])
                filename = "_".join([str(polygon_id)] + self.source['name'] + [time_stamp])
                dest_path = "/".join(filename_parts + [filename])

                self.export_params = {
                    'bucket': self.export_folder,
                    'resolution': self.source['resolution'],
                    'filename': filename,
                    'dest_path': dest_path
                }

                date_range = self.date_range_list[i]

                self.current_poly = {
                    'date_range': date_range,
                    'polygon_id': polygon_id,
                    'roi': roi
                }


                if self.dynamic_date_range is False:
                    self.skip_test_check = True
                else:
                    self.skip_test_check = False
                        
                self._export_try_except_loop(attempts=0)

                if self.debug:
                    logging_timestamp = "_".join(time.ctime().split(" ")[1:])
                    logging.info(f'{logging_timestamp}: Polygon {polygon_id} successfully processed')

        return self.exports, self.exceptions

    def _export_try_except_loop(self,attempts):
        '''
        Helper function that enables the pipeline to wait a certain number of minutes after a GEE error
        instead of erroring out
        '''
        try:
            # export_single_feature function not created for object yet
            export = self.export_single_feature()
            self.exports.append(export)
        except Exception as e:
            if self.debug:
                attempts += 1
                logging_timestamp = "_".join(time.ctime().split(" ")[1:])
                logging.info(f'{logging_timestamp}: Timeout #{attempts}; retrying in {self._minutes_to_wait} minutes')
            print(f'{e}; please wait for {self._minutes_to_wait} minutes', end='\r', flush=True)
            self.exceptions.append(e)
            # wait 60 minutes
            time.sleep(60 * self._minutes_to_wait)

            self.export_try_except_loop(attempts=attempts)

    # def makeImageCollection(self,ee_roi):
    #     '''
    #     Makes a GEE ImageCollection object based off of an input GEE Geometry Polygon object, the date range
    #     of the current polygon under consideration (based off of the Date Range List), and then applies
    #     the necessary Cloudfree Merging functions to it. Filters are also applied based on the configuration file.

    #     "ee_roi": A GEE Geometry Polygon object specifying the bounds of the image query.

    #     The following functions are applied to each image in the query:
    #         inject_B10: If the images are from Sentinel-2 L2A, they lack the 'B10' band; this function finds the matching
    #                     L1C image and gets its B10 band.
    #         sentinel2CloudScore: Makes a cloud mask
    #         calcCloudCoverage: Calculates the cloud coverage of the image based off of the cloud mask
    #         sentinel2ProjectShadows: Makes a mask of cloud shadows
    #         computeQualityScore: Computes the Quality Pixel Percentage score for the image

    #     '''
    #     ## Split into the original call/filters and the mapping
    #     filters_before, filters_after = self._makeFilterList()
    #     #print(modifiers)

    #     start_date = self.current_poly['date_range']['start_date']
    #     end_date = self.current_poly['date_range']['end_date']

    #     collection = ee.ImageCollection(self.sensor['name']) \
    #                 .filterDate(ee.Date(start_date), ee.Date(end_date)) \
    #                 .filterBounds(ee_roi) \
    #                 .map( lambda x: clipToROI(x, ee.Geometry(ee_roi)) )
    
    #     #print("size of collection:",collection.size().getInfo())

    #     modifiers = []
    #     if self.sensor['name'].lower() == "copernicus/s2_sr":
    #         #print('Inject B10')
    #         modifiers.append(inject_B10)

    #     modifiers += [sentinel2CloudScore, calcCloudCoverage, sentinel2ProjectShadows, computeQualityScore]

    #     if filters_before is not None:
    #         collection = collection.filter( filters_before )
        
    #     for m in modifiers:
    #         #print(f'Applying modifier {m}')
    #         collection = collection.map(m)

    #     if filters_after:
    #         collection = collection.filter( filters_after )

    #     return collection



    def mergeCollection(self, imgC, test_coll=False):
        '''
        Uses Cloudfree Merging to merge a GEE ImageCollection into a single cloud-free image.

        "imgC": The ImageCollection you want to merge

        "test_coll": Boolean that determines whether or not to test the collection to see if it has a
        sufficient number of quality images. Used for Dynamic Date Range. Defaults to False.
        '''

    # Select the best images, which are below the cloud free threshold, sort them in reverse order (worst on top) for mosaicing
        ## same as the JS version
        # logging.info(f'---POLYGON {polygon_id}---')
        # logging.info(f'{date_range["start_date"]} to {date_range["end_date"]}')
        # logging.info(f'Collection size: {imgC.size().getInfo()}')

        filterBy='NON_QUALITY_PIXEL_PERCENTAGE'
        secondary_sort='CLOUDY_PIXEL_PERCENTAGE' 

        best = imgC.filterMetadata(filterBy, operator='less_than', value=self.qual_img_thresh)
        
        if test_coll:
            coll_is_good = collection_greater_than(best, self.qual_img_amount)

            # print('Coll is good:', coll_is_good)

            if not coll_is_good:
                return None
        
        # best_sorted = image_collection_secondary_sort(best,primary_sort=filterBy,secondary_sort=secondary_sort)
        best_sorted = best.sort(secondary_sort, False).sort(filterBy, False)

        # logging.info(f'Best size: {best.size().getInfo()}')

        # best_filtered = best.filterMetadata('NODATA_PIXEL_PERCENTAGE', 'less_than', 10)

        # logging.info(f'Filtered best size: {best_filtered.size().getInfo()}')
        # logging.info('')
        # logging.info('')

    #     return collection_quality_test_filter(imgC, best)
    #     print('Info on first image of collection:', imgC.first().getInfo())
        filtered = imgC.qualityMosaic(qualityBand='cloudShadowScore')

        # Add the quality mosaic to fill in any missing areas of the ROI which aren't covered by good images
        newC = ee.ImageCollection.fromImages( [filtered, best_sorted.mosaic()] )
        
    #     print("collection merged")

        return ee.Image(newC.mosaic())

    def _expand_date_range(self):
        '''
        Helper function that expands the date range to the next day_offset level. Used in Dynamic Date Range.
        '''

        original_date = self.current_poly['date_range']['original_date']
        day_offset = self.current_poly['date_range']['day_offset']


        new_offset = self.offset_dict[day_offset]

        start = original_date + DateOffset(days=-day_offset)
        end = original_date + DateOffset(days=day_offset)
        start_date = str(start)[:10]
        end_date = str(end)[:10]

        new_date_range = {
            'start_date': start_date,
            'end_date': end_date,
            'original_date': original_date,
            'day_offset': new_offset
        }

        return new_date_range

    def _add_ndvi_band(self,img):
        '''
        Helper function that calculates the NDVI values for a GEE Image and adds it
        to that image as a band.

        "img": The image you want the NDVI band added to
        '''
        ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
        cloudFree = img.addBands(ndvi)
        cloudFree = cloudFree.float()

        return cloudFree


    def export_single_feature(self):
        '''
        Based off of the current polygon and its date range, queries for images, merges them, and exports
        the resulting cloud-free image.

        The following functions are applied to each image in the query:
            inject_B10: If the images are from Sentinel-2 L2A, they lack the 'B10' band; this function finds the matching
                        L1C image and gets its B10 band.
            sentinel2CloudScore: Makes a cloud mask
            calcCloudCoverage: Calculates the cloud coverage of the image based off of the cloud mask
            sentinel2ProjectShadows: Makes a mask of cloud shadows
            computeQualityScore: Computes the Quality Pixel Percentage score for the image
        '''
        modifiers = []
        if self.sensor['name'].lower() == "copernicus/s2_sr":
            #print('Inject B10')
            modifiers.append(inject_B10)
        modifiers += [sentinel2CloudScore, calcCloudCoverage, sentinel2ProjectShadows, computeQualityScore]

        ee_dir = self.sensor['name']
        roi_ee = ee.Geometry.Polygon(self.current_poly['roi'])
        start_date = self.current_poly['date_range']['start_date']
        end_date = self.current_poly['date_range']['end_date']
        filters_before, filters_after = self._makeFilterList()

        imgC = makeImageCollection(ee_dir, roi_ee, start_date, end_date, filters_before, modifiers, filters_after)

        #print(f'Size of polygon {polygon_id}: {imgC.size().getInfo()}')
        # print(imgC.size().getInfo())
        # return None
        ## sort was not in the original version
        #image_collection = image_collection.sort(sort_by)
        ## below line was in the original verson;
        ## changing to the JS version
        ## img = image_collection.mosaic().clip(roi_ee)



        # finds final offset 
        max_offset = self.offset_dict[max(self.offset_dict.keys())]

        if self.skip_test_check is True or (self.current_poly['date_range']['day_offset'] == max_offset):
            cloudFree = self.mergeCollection(imgC, test_coll=False)
        else:
            cloudFree = self.mergeCollection(imgC, test_coll=True)

        if cloudFree is None:
            self.current_poly['date_range'] = self._expand_date_range()
            return self.export_single_feature()

        else:

            cloudFree = cloudFree.clip(roi_ee).reproject('EPSG:4326', None, 10)

            ## make NDVI band
            cloudFree = self._add_ndvi_band(img=cloudFree)


            self.export_params['img'] = cloudFree
            self.export_params['roi'] = self.current_poly['roi']
            self.export_params['sensor_name'] = self.sensor['name'].lower()
            self.export_params['bands'] = self.sensor['bands']
            
            return self.exportImageToGCS()


    def exportImageToGCS(self, start=True):
        '''
        Exports an image to your Google Cloud Services account based off of the
        current export_params attribute.

        "start": Boolean. Set to False if you don't want to actually export the image. Defaults to True.
        '''

        img = self.export_params['img']
        bands = self.export_params['bands']
        filename = self.export_params['filename']
        resolution = self.export_params['resolution']
        roi = self.export_params['roi']
        dest_path = self.export_params['dest_path']
        bucket = self.export_params['bucket']


        img = img.select(bands)
            

        export = ee.batch.Export.image.toCloudStorage(
            image=img,
            description=filename,
            scale=resolution,
            region=roi,
            fileNamePrefix=dest_path,
            bucket=bucket,
            maxPixels=1e13
            )
        

        if start:
            export.start()
            
        return export






#         def mergeCollection(imgC, keepThresh=5, filterBy='CLOUDY_PERCENTAGE',secondary_sort='CLOUDY_PIXEL_PERCENTAGE' ,filterType='less_than', mosaicBy='cloudShadowScore', polygon_id=None, date_range=None, test_coll=False):
#             best = imgC.filterMetadata(filterBy, filterType, keepThresh)
            
#             if test_coll:
#                 coll_is_good = collection_greater_than(best, 5)

#                 if not coll_is_good:
#                     return None
            
#             best_sorted = best.sort(secondary_sort, False).sort(filterBy, False)

#             filtered = imgC.qualityMosaic(mosaicBy)

#             newC = ee.ImageCollection.fromImages( [filtered, best_sorted.mosaic()] )

#             return ee.Image(newC.mosaic())


#         def exportImageToGCS(img=None, roi=None, bucket=None, filename=None, dest_path=None, resolution=10, start=True, sensor_name=None, bands=None):
#             img = img.select(bands)

#             export = ee.batch.Export.image.toCloudStorage(
#                 image=img,
#                 description=filename,
#                 scale=resolution,
#                 region=roi,
#                 fileNamePrefix=dest_path,
#                 bucket=bucket,
#                 maxPixels=1e13
#             )

#             if start:
#                 export.start()

#             return export


# def export_single_feature(offset_dict, roi=None, sensor=None, date_range=None, export_params=None, sort_by='CLOUDY_PIXEL_PERCENTAGE', polygon_id=None, area_limit=1000, skip_test=True, tile=None):
#     modifiers = []
#     if sensor['name'].lower() == "copernicus/s2_sr":
#         #print('Inject B10')
#         modifiers.append(inject_B10)
#     if sensor['type'].lower() == "opt":
#         #print(sensor['type'])
#         modifiers += [sentinel2CloudScore, calcCloudCoverage, sentinel2ProjectShadows, computeQualityScore]
#         #print(modifiers)
                    
#     #print(f'Tile is {tile}')

#     roi_ee = ee.Geometry.Polygon(roi)

#     imgC = makeImageCollection(sensor, roi_ee, date_range['start_date'], date_range['end_date'], modifiers=modifiers, tile=tile)

#     #print(f'Size of polygon {polygon_id}: {imgC.size().getInfo()}')
#     # print(imgC.size().getInfo())
#     # return None
#     ## sort was not in the original version
#     #image_collection = image_collection.sort(sort_by)
#     ## below line was in the original verson;
#     ## changing to the JS version
#     ## img = image_collection.mosaic().clip(roi_ee)

#     if skip_test is True:
#         cloudFree = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=False)
#     elif (date_range['day_offset'] == 'two years'): #or (date_range['area'] >= area_limit)
#         # If we're pulling from two years, we'll end the dynamic date range loop and just have
#         # this collection be the final one.
#         cloudFree = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=False)
#     else:
#         cloudFree = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=True)
#         # col1,col2 = mergeCollection(imgC, polygon_id=polygon_id, date_range=date_range, test_coll=True)
#         # return col1,col2

#     if cloudFree is None:
#         original_date = date_range['original_date']
#         area = date_range['area']
#         day_offset = date_range['day_offset']

#         # offset_dict = {
#         #     45: 90,
#         #     90: 180,
#         #     180: 'two years'
#         # }

#         # offset_dict = {
#         #     30: 'two years'
#         # }

#         new_offset = offset_dict[day_offset]

#         if new_offset == 'two years':
#             start_date = '2019-01-01'
#             end_date = '2020-12-31'
#         else:
#             start = original_date + DateOffset(days=-day_offset)
#             end = original_date + DateOffset(days=day_offset)
#             start_date = str(start)[:10]
#             end_date = str(end)[:10]

#         # if tile is None:
#         #     print(f'Polygon {polygon_id} is increasing offset to {new_offset}')
#         # else:
#         #     print(f'Tile {tile} is increasing offset to {new_offset}')

#         new_date_range = {
#             'start_date': start_date,
#             'end_date': end_date,
#             'original_date': original_date,
#             'day_offset': new_offset,
#             'area': area
#         }

#         return export_single_feature(
#                     offset_dict=offset_dict,
#                     roi=roi,
#                     sensor=sensor,
#                     date_range=new_date_range,
#                     export_params=export_params,
#                     sort_by=sort_by,
#                     polygon_id=polygon_id,
#                     tile=tile,
#                     skip_test=False
#                 )

#     else:
#         # if tile is None:
#         #     print(f'Polygon {polygon_id} successfully merged with offset {date_range["day_offset"]}')
#         # else:
#         #     print(f'Tile {tile} successfully merged with offset {date_range["day_offset"]}')

#         if tile is None:
#             ## when doing a feature collection
#             # print('clipping to ROI in export_single_feature')
#             cloudFree = cloudFree.clip(roi_ee).reproject('EPSG:4326', None, 10)
#         else:
#             ## when doing tile by tile
#             # print('not clipping in export_single_feature')
#             cloudFree = cloudFree.reproject('EPSG:4326', None, 10)
#         ## Do we need to mosaic it now???
#         # print('cloudFree info:', cloudFree.getInfo())
#         # print('Mosaic type:', type(img))

#         ## make NDVI band
#         ndvi = cloudFree.normalizedDifference(['B8', 'B4']).rename('NDVI')
#         cloudFree = cloudFree.addBands(ndvi)
#         cloudFree = cloudFree.float()

#         new_params = export_params.copy()
#         new_params['img'] = cloudFree
#         new_params['roi'] = roi
#         new_params['sensor_name'] = sensor['name'].lower()
#         new_params['bands'] = sensor['bands']
        
#         return exportImageToGCS(**new_params)

# def _serialise_task_log(task_log):
#     for k,v in task_log.items():
#         task_log[k]['task_def']['action'] = "export_single_feature"

#     return task_log

# def load_task_log(filename='task_log.json'):
#     with open(filename, 'r') as f:
#         task_log = json.load(f)

#     for k, v in task_log.items():
#         task_log[k]['task_def']['action'] = globals()[task_log[k]['task_def']['action']]

#     return task_log

# def monitor_tasks(task_log):
#     print("SAVING LOG")
#     f_raw = open('task_log.json', 'w')
#     with FileObjectThread(f_raw, 'w') as handle:
#         task_log = _serialise_task_log(task_log)
#         json.dump(task_log, handle)

#     f_raw.close()

# # def load_config(path):
# #     with open(path, 'r') as stream:
# #         try:
# #             return yaml.load(stream)
# #         except yaml.YAMLError as exc:
# #             print(exc)



# if __name__ == "__main__":
#     parser = ArgumentParser()
#     parser.add_argument("-c", "--config", default=None, help="Config file for the download")
#     args = parser.parse_args()

#     assert args.config, "Please specify a config file for the download"
#     config = load_config(args.config)
#     print(config)

#     ee.Initialize()

#     task_queue = GEETaskManager(n_workers=config['max_tasks'], max_retry=config['max_retry'], wake_on_task=True, log_file=config['log_file'], process_timeout=config['task_timeout'])
#     task_queue.register_monitor(monitor_tasks)

#     if os.path.exists('task_log.json'):
#         task_log = load_task_log(filename='task_log.json')
#         task_queue.set_task_log(task_log)

#     pre_mosaic_sort = config['pre_mosaic_sort']
#     for data_list in config['data_list']:
#         for sensor_idx in data_list['sensors']:
#             sensor = config['sensors'][sensor_idx]
#             tasks = process_datasource(task_queue, data_list, sensor, config['export_to'], config['export_dest'], pre_mosaic_sort)

#     print("Waiting for completion...")
#     task_queue.wait_till_done()
