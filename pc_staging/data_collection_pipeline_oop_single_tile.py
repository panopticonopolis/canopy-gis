# Sentinel Hub Config
#from env_vars import sentinel_hub_instance_id
from sentinelhub import SHConfig
# Import Area of Interest List
import pandas as pd
import json
import mgrs
# Sentinel Hub Tile Look Up / Download
from sentinelhub import WebFeatureService, BBox, CRS, DataSource, AwsTileRequest
# Cloud Masking
import rasterio as rio
import numpy as np
import earthpy.mask as em
# Generate Product Detail DataFrame
import os
from glob import glob
import xml.etree.ElementTree as ET
# Sort / Organize Tiles by Individual Folders, All Directory Deletions
from shutil import copyfile, rmtree, move, copy
# Reproject Masked Files 
import gdal
from glob import glob
# Create Master Raster
import pandas as pd
from shapely.geometry import Polygon
import geopandas as gpd
from geopandas import GeoDataFrame
import earthpy.spatial as es
import traceback
# TIF to JPG
from PIL import Image


class CollectionPipeline:
    def __init__(self, shub_instance_id, bounding_box, tile_list, search_interval, output_dir,
                 num_layers=10, product_type=DataSource.SENTINEL2_L2A, S3=False, bands=["R10m/TCI"],
                 epsg_warp_format='EPSG:4326', product_ordering='Cloud Cover',
                 mask_threshold=0, windows=False, chip_size=[100,100],
                 raw_dir_s3_dest=None, chips_dir_s3_dest=None):
        self.shub_instance_id = shub_instance_id
        self.bounding_box = bounding_box
        self.tile_list = tile_list
        self.search_time_interval = search_interval
        self.output_dir = self._add_trailing_slash(output_dir)
        self.raw_dir = self.output_dir + 'raw/'
        self.masked_dir = self.output_dir + 'masked/'
        self.ordered_dir = self.output_dir + 'ordered/'
        self.warped_dir = self.output_dir + 'ordered_warped/'
        self.temp_vrt_dir = self.output_dir + 'temp_virtual_rasters/'
        self.full_vrt_dir = self.output_dir + 'full_virtual_rasters/'
        self.tile_tif_dir = self.output_dir + 'tile_tifs/'
        self.mosaic_dir = self.output_dir + 'master_raster/'
        self.chips_dir = self.output_dir + 'chips/'
        self.num_layers = num_layers
        self.product_type = product_type
        self.S3 = S3
        self.bands = bands
        self.epsg_warp_format = epsg_warp_format
        self.product_ordering = product_ordering
        self.mask_threshold = mask_threshold
        self.windows = windows
        self.chip_size = chip_size
        self.raw_dir_s3_dest = raw_dir_s3_dest
        self.chips_dir_s3_dest = chips_dir_s3_dest

        
    def run(self, tiles=None, start_step='download', end_step='upload'):
        '''
        Steps:
        1. Download
        2. Process
        3. Chip
        4. Upload
        '''
        step_dict = {
            'download': 1,
            'process': 2,
            'chip': 3,
            'upload': 4
        }

        if not tiles:
            tiles = self.tile_list
            if not tiles: 
                raise ValueError(
                    f'Tile list does not contain an array of tile ids')

        if start_step.lower() not in step_dict.keys():
            raise ValueError(f'''Start Step {start_step} invalid.
                             Must be one of the following: download; process; chip; upload''')
        else:
            start = step_dict[start_step.lower()]
        if end_step.lower() not in step_dict.keys():
            raise ValueError(f'''End Step {end_step} invalid.
                             Must be one of the following: download; process; chip; upload''')
        else:
            end = step_dict[end_step.lower()]

        if start > end:
            raise ValueError('Must end at a later step than when you start')

        self._create_init_dirs()
        gdal.UseExceptions()
        if start <= 1 and end >= 1:
            self.download_products()

        if start <= 2 and end >= 2:
            for index,tile in enumerate(tiles,1):
                print(f'Starting single tile pipeline for tile {index} of {len(tiles)}')
                self.single_tile_process(tile)
                print(f'Finished single tile GeoTIFF for tile {index} of {len(tiles)}')

        if start <= 3 and end >= 3:
            self.main_tiff_process()

        if start <= 4 and end >= 4:
            if self.raw_dir_s3_dest is not None:
                self._copy_to_existing_directory(self.raw_dir, self.raw_dir_s3_dest)

            if self.chips_dir_s3_dest is not None:
                self._copy_to_existing_directory(self.chips_dir, self.chips_dir_s3_dest)



    def download_products(self):
        print('Connecting to Sentinel Hub')
        self.config = self.shub_connect(self.shub_instance_id)
        print('Searching...')
        self.results2 = self.shub_lookup_tiles(self.bounding_box, self.tile_list)
        print('Downloading products')
        self.shub_download_tiles(self.results2, self.bands)

    
    def single_tile_process(self,tile):
        print(f'Applying mask to tile {tile}')
        single_tile_list = glob(self.raw_dir + "/" + 
                                f'*{tile}*/*/R10m/*.jp2')
        print('Tile list:', single_tile_list)
        if len(single_tile_list) == 0:
            print('No tiles of this ID were downloaded')
        else:
            for raw_image_path in single_tile_list:
                self._cloud_mask_tci(raw_image_path)

            print('Making metadata dataframe')
            self.metadata_df = self.generate_product_detail_df(tile,self.product_ordering)
            print('Ordering products')
            self.order_masked_tiles()
            print('Deleting Masked products')
            self._delete_contents_of_dir(self.masked_dir)
            print('Warping products')
            self.convert_rasters(self.epsg_warp_format)
            print('Deleting Masked Ordered Products')
            self._delete_contents_of_dir(self.ordered_dir)
            print('Making virtual rasters')
            self.make_tile_virtual_raster()
            print('Making GeoTIFF')
            self.vrt_to_tif(tile)
            print('Deleting Warped Ordered Products')
            self._delete_contents_of_dir(self.warped_dir)
            print('Deleting Tile Virtual Rasters')
            self._delete_contents_of_dir(self.temp_vrt_dir)

    def main_tiff_process(self):
        print("Creating Master VRT for Tile GeoTIFFs")
        self.make_full_virtual_raster()
        self.vrt_to_tif()
        print("Deleting individual Tile GeoTIFFs")
        self._delete_contents_of_dir(self.tile_tif_dir)
        print("Chipping Master GeoTIFF")
        self.chip_master_tif()


    def _add_trailing_slash(self, path):
        if path[-1] != '/':
            path += '/'
        return path


    def _create_dir(self, output_dir):
        # If the output folder doesn't exist, create it
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)


    def _create_init_dirs(self):
        for directory in [self.output_dir, self.raw_dir, self.masked_dir, 
                          self.ordered_dir,self.warped_dir, 
                          self.temp_vrt_dir, self.full_vrt_dir,
                          self.tile_tif_dir, self.mosaic_dir, self.chips_dir
                          ]:
            self._create_dir(directory)

    def _delete_contents_of_dir(self,input_dir):

        for root, dirs, files in os.walk(input_dir):
       
            for name in files:
                    print(os.path.join(root, name)) 
                    os.remove(os.path.join(root, name))
            for name in dirs:
                    print(os.path.join(root, name))
                    rmtree(os.path.join(root, name))


    def _copy_to_existing_directory(self,src, dest, ignore=None):

        if os.path.isdir(src):
            if not os.path.isdir(dest):
                os.makedirs(dest)
            files = os.listdir(src)
            if ignore is not None:
                ignored = ignore(src, files)
            else:
                ignored = set()
            for f in files:
                if f not in ignored:
                    self._copy_to_existing_directory(os.path.join(src, f), 
                                        os.path.join(dest, f), 
                                        ignore)
        else:
            copyfile(src, dest)


    def shub_connect(self, shub_instance_id):
        INSTANCE_ID = shub_instance_id  

        if INSTANCE_ID:
            config = SHConfig()
            config.instance_id = INSTANCE_ID
        else:
            config = None
        
        return config


    def shub_lookup_tiles(self, bounding_box, tile_list):
    
        #Misha's Tiles of Interest
        search_bbox = BBox(bbox=bounding_box, crs=CRS.WGS84)

        start_time = self.search_time_interval[0] + 'T00:00:00'
        end_time = self.search_time_interval[1] + 'T23:59:59'
        search_time_interval = (start_time, end_time)

        wfs_iterator = WebFeatureService(
            search_bbox,
            search_time_interval,
            data_source=self.product_type,
            maxcc=.05,
            config=self.config
        )
        results = wfs_iterator.get_tiles()
        df = pd.DataFrame(results, columns=['Tilename','Date','AmazonID'])
        df_tiles_of_interest = df[df["Tilename"].isin(tile_list)]
        undownloaded_tiles = []
        for tile in tile_list:
            if tile not in df_tiles_of_interest['Tilename'].unique():
                undownloaded_tiles.append(tile)
        if len(undownloaded_tiles) > 0:
            print('The following tiles were not downloaded:', undownloaded_tiles)
        df2 = df_tiles_of_interest.groupby('Tilename').head(self.num_layers)
        results2 = list(df2.itertuples(index=False,name=None))
        return results2


    def shub_download_tiles(self, results_list, bands):
    
        self._create_dir(self.raw_dir)
    
        length = len(results_list)
        for i, tile in enumerate(results_list, 1):
            print(f'Downloading tile {i} of {length}: {tile}')
            tile_name, time, aws_index = tile

            #Download SAFE Files
            request = AwsTileRequest(
                tile=tile_name,
                time=time,
                bands=bands, 
                aws_index=aws_index,
                data_folder=self.raw_dir,
                data_source=self.product_type,
                safe_format=True
            )

            request.save_data(redownload=True)


    def _cloud_mask_tci(self, raw_image_path):
        '''
        '''
        if self.windows:
            prod_dir = "/".join(raw_image_path.split("\\")[:-3])
        else:
            prod_dir = "/".join(raw_image_path.split("/")[:-3])
        prod_dir = self._add_trailing_slash(prod_dir)
        msk_file_path = glob(prod_dir + "QI_DATA/MSK_CLDPRB_20m.jp2")[0]
        print(msk_file_path)
        
        print(f'Applying mask to file {raw_image_path}')
        
        nodatavalue = int(0)
               
        if self.windows:
            tci_filename = raw_image_path.split('\\')[-1]
        else:
            tci_filename = raw_image_path.split("/")[-1]
        
        output_tci_file_path = self.masked_dir + "/" + "processed_" + tci_filename
        print(output_tci_file_path)
        
        
        with rio.open(raw_image_path) as sen_TCI_src:
            sen_TCI = sen_TCI_src.read(masked=True)
            sen_TCI_meta = sen_TCI_src.meta
        
        with rio.open(msk_file_path) as sen_mask_src:
            sen_mask_pre = sen_mask_src.read(1)
            sen_mask = np.repeat(np.repeat(sen_mask_pre,2,axis=0),2,axis=1)

        # All pixels above 0 probability will be classified as True

        sen_mask_qa = sen_mask > self.mask_threshold

        # Apply mask to source TCI file
        if np.count_nonzero(sen_mask_qa) > 0:
            sen_TCI_cl_free_nan = em.mask_pixels(sen_TCI, sen_mask_qa)
            sen_TCI_cl_free_processed = np.ma.filled(sen_TCI_cl_free_nan, fill_value=nodatavalue)
        else:
            sen_TCI_cl_free_processed = sen_TCI

        #print('sen TCI shape:', sen_TCI_cl_free_processed.shape)
        # Export cloud-masked TCI file
        with rio.open(output_tci_file_path, 'w', **sen_TCI_meta) as outf:
            outf.write(sen_TCI_cl_free_processed)



    def apply_mask_tci_safe_list(self):
        '''
        products_dir refers to parent directory containing multiple products
        '''
        
        dir_list = glob(self.raw_dir + '*')
        length = len(dir_list)
        for i, directory in enumerate(dir_list, 1):
            print(f'Applying mask to product {i} of {length}; location {directory}')
            self._cloud_mask_tci(directory)
            
        #print(f"Applied masks to {len(dir_list)} products")


    def generate_product_detail_df(self,tile_id, sort_by):
        '''
        Generate product details dataframe used as input for ordering products by Cloudy Pixel Percentage,
        No Data Pixel Percentage, or Unclassified Percentage
        '''
        dir_paths = glob(self.raw_dir + "*/")
        dir_paths = [i for i in dir_paths if tile_id in i]

        meta_data = []
        for path in dir_paths:
            if self.windows:
                folder = path.split('\\')[-2]
            else:
                folder = path.split("/")[-2]
            xml_loc = glob(self.raw_dir + folder + "/*.xml")[0]
            tree = ET.parse(xml_loc)
            directory = [elem.text for elem in tree.iter() if "MASK_FILENAME" in elem.tag][0].split("/")[1]
            tile_id = directory.split("_")[1]
            filepath_partial = self.raw_dir + directory + "/IMG_DATA" + "/R10m"
            filepath_origin = glob(filepath_partial + f"/*.jp2")[0]
            print('Filepath Origin:', filepath_origin)
            if self.windows:
                filename = filepath_origin.split('\\')[-1]
            else:
                filename = filepath_origin.split("/")[-1]
            filepath_output = (self.masked_dir + "processed_" + filename)
            filename = filepath_output.split("/")[-1]
            cloud_cover,no_data,unclassified = [float(elem.text) for elem in tree.iter() if "CLOUDY_PIXEL_PERCENTAGE" in elem.tag 
                    or "NODATA_PIXEL_PERCENTAGE" in elem.tag or "UNCLASSIFIED_PERCENTAGE" in elem.tag]
            meta_data.append([directory,tile_id,cloud_cover,no_data,unclassified,filename,filepath_output])
        df = pd.DataFrame(meta_data,columns=["Directory","Tile_Id","Cloud Cover","No Data Percentage","Unclassified Percentage","Filename","Filepath"])
        if sort_by == 'Cloud Cover':
            sort_by_2 = 'Unclassified Percentage'
        elif sort_by == 'Unclassified Percentage':
            sort_by_2 = 'Cloud Cover'
        df2 = df.sort_values(by=["Tile_Id",sort_by,sort_by_2],ignore_index=True)

        return df2


    def order_masked_tiles(self):    
        '''
        df input is the products detail pre-sorted dataframe to be used for sorting products 
        '''
        df = self.metadata_df
        layer = 1
        for index,row in df.iterrows(): 
            destination_dir = self.ordered_dir + str(layer)
            output_file = destination_dir + "/" + row["Filename"]

            self._create_dir(destination_dir)

            print('src_dr', row['Filepath'])
            # Copy file to existing or new directory
            copyfile(row["Filepath"],output_file)

            # Check if Tile_Id already exists in the directory - only necessary up until the last tile
            if len(df) > index + 1:
                if df.loc[index,"Tile_Id"] == df.loc[index + 1,"Tile_Id"]:
                    layer += 1
                else:
                    layer = 1


    def convert_rasters(self, epsg_format):
        """Converts the rasters in the src_dir into a different EPSG format,
        keeping the same folder structure and saving them in the dest_dir."""

        src_dir = self.ordered_dir
        dest_dir = self.warped_dir

        input_files = glob(src_dir + '*/*.jp2')
        # Keep track of how many files were converted
        total = len(input_files)
        
        for i, f in enumerate(input_files, 1):
            print(f'processing file {i} of {total}; location: {f}')
            
            # The way we've set it up, we save each product into a numbered folder,
            # depending on which layer it's in. To keep this structure, we need to
            # pull out the folder number from the file path.
            # How exactly to do this depends on if you're using Windows or not,
            # since the path conventions are different.
            if self.windows:
                folder_num = f.split('\\')[-2]
                filename = f.split('\\')[-1]
            else:
                folder_num = f.split('/')[-2]
                filename = f.split('/')[-1]
            output_folder = dest_dir + folder_num + '/'
            
            
            # If the respective grouping folders are not available 
            self._create_dir(output_folder)
            
            output_filepath = output_folder + filename
            
            #print(output_filepath)
            #print(f)

            # Finally, we convert
            converted = gdal.Warp(output_filepath, [f], format='GTiff',
                                dstSRS=epsg_format, resampleAlg='near')
            converted = None
            
        #print('Finished')


    def make_tile_virtual_raster(self):
        """Combines the rasters in the src_dir into a single virtual raster
        with proper prioritization. This is saved into the dest_dir.
        Make sure the num_layers variable is the same as the number of tile layers
        in your src_dir."""
        
        src_dir = self.warped_dir
        vrt_dir = self.temp_vrt_dir
        
                
        for layer in range(1, self.num_layers+1):
            print('Making Layer', layer)
            
            # Get the filenames from the layer in question
            filenames = glob(src_dir + f'{layer}/*.jp2', recursive=True)
            print(filenames)
            
            output_file = vrt_dir + f'Layer{layer}.vrt'
            print(output_file)
        
            vrt = gdal.BuildVRT(output_file, filenames, resolution='average', resampleAlg='nearest', srcNodata=0)
        
            vrt.FlushCache()
        
        print('Making full raster')

        # To make the full raster, we combine every layer. Do it in reverse order because (I believe)
        # the last items in the list are prioritized.

        input_files = [vrt_dir + f'Layer{i}.vrt' for i in reversed(range(1, self.num_layers+1))]
        
        output_file = vrt_dir + 'full.vrt'

        vrt = gdal.BuildVRT(output_file, input_files, resolution='average', resampleAlg='nearest', srcNodata=0)

        vrt.FlushCache()

        #print('Finished')

    def make_full_virtual_raster(self):
        """Combines the individual tile rasters in the master_raster directory"""
        
        src_dir = self.tile_tif_dir
        vrt_dir = self.full_vrt_dir

        filenames = glob(src_dir + "/*")
        print(filenames)
            
        output_file = vrt_dir + 'full_vrt.vrt'
        print(output_file)
        
        vrt = gdal.BuildVRT(output_file, filenames, resolution='average', resampleAlg='nearest', srcNodata=0)
        
        vrt.FlushCache()

        print('Finished')


    def vrt_to_tif(self,tile=None):

        if tile:
            translate = gdal.Translate(self.tile_tif_dir + f'T{tile}_full_tif.tif', self.temp_vrt_dir + 'full.vrt', format='GTiff')
        if not tile:
            translate = gdal.Translate(self.mosaic_dir + 'full_tif.tif', self.full_vrt_dir + 'full_vrt.vrt', format='GTiff')

        translate.FlushCache()
    
    def chip_master_tif(self):

        in_path_tiff = self.mosaic_dir + 'full_tif.tif'
        out_path_dir = self.chips_dir

        tile_size_x = self.chip_size[0]
        tile_size_y = self.chip_size[1]

        ds = gdal.Open(in_path_tiff)
        band = ds.GetRasterBand(1)
        xsize = band.XSize
        ysize = band.YSize


        for i in range(0, xsize, tile_size_x):
            for j in range(0, ysize, tile_size_y):

                gdal.Translate(out_path_dir + 'full_tif' + '_' + str(i) + "_" + str(j) + ".tif",in_path_tiff, srcWin = [i, j, tile_size_y, tile_size_x],format='GTiff')




class LabellingPipeline:
    def __init__(self, csv_loc, windows=False):
        self.csv_loc = csv_loc
        self.windows = windows


    def _add_trailing_slash(self, path):
        if path[-1] != '/':
            path += '/'
        return path


    def _create_dir(self, output_dir):
        # If the output folder doesn't exist, create it
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)


    def import_aois(self):    
        csv_loc = self.csv_loc

        df_labels = pd.read_csv(csv_loc)
        df_labels = df_labels[["center-lat","center-long","polygon","Labels combined"]]

        polygons = []
        for polygon in df_labels["polygon"]:
            polygons.append(json.loads(polygon)["coordinates"])


        tiles = []
        tiles_dic = {}
        polygon_id = 0 
        coordinates = []
        m = mgrs.MGRS()
        for items in polygons:
            polygon_id += 1 
            for item in items:
                for lon_lat in item:
                    coordinates.append(lon_lat)
                    c = m.toMGRS(lon_lat[1], lon_lat[0])
                    tile = c[0:5]
                    

                    if polygon_id in tiles_dic:

                        tiles_dic[polygon_id].append(tile)

                    else:

                        tiles_dic[polygon_id] = [tile]

                    tiles.append(tile)

                tiles_dic[polygon_id] = list(set(tiles_dic[polygon_id]))

        tiles = list(set(tiles))

        df_labels["tiles"] = tiles_dic.values()

        #bounding box

        min_lon = min([i[0] for i in coordinates])
        min_lat = min([i[1] for i in coordinates])
        max_lon = max([i[0] for i in coordinates])
        max_lat = max([i[1] for i in coordinates])

        bounding_box = min_lon,min_lat,max_lon,max_lat
        
        return bounding_box, tiles


    def csv_to_gdf(self):
        '''
        import manually created areas of interest csv
        
        output is an in-memory geo dataframe with one polygon AOI per row to be utilized for cropping master raster
        
        '''
        csv_loc = self.csv_loc
        df = pd.read_csv(csv_loc)
        df_labels = df[["polygon","Labels combined"]]

        #create geometry column for polygons
        polygons = []
        for polygon in df_labels["polygon"]:
            polygon_temp = []
            for coordinates in json.loads(polygon)["coordinates"]:
                for coordinate in coordinates:
                    polygon_temp.append(tuple(coordinate))
                polygons.append(Polygon(polygon_temp))

        gdf_series = gpd.GeoSeries(polygons)
        gdf = gpd.GeoDataFrame(gdf_series,geometry=0)
        gdf["geometry"] = gdf[0]
        gdf = gdf.drop(columns=[0])
        
        # add Labels column 
        gdf["Labels"] = [s.strip().split(", ") for s in df_labels["Labels combined"]]
        
        return gdf


    def export_aoi_polygon_rasters(self, gdf, master_raster_path, output_dir):
        
        output_parent_dir = self._add_trailing_slash(output_dir) 
        
        # create parent output directory if it doesn't exist
        self._create_dir(output_dir)

        src_raster_file = rio.open(master_raster_path)
        
        for index in range(gdf.shape[0]):
            
            crop_extent = gdf.loc[[index],"geometry"]
            

            try:
                raster_crop, raster_meta = es.crop_image(src_raster_file, crop_extent)
    #             print(f"succesfully cropped image {index} ")
                
            except Exception:
                
                print(f"polygon on row {index} does not overlap with master raster, continuing")
                traceback.print_exc()
                
            

            # Update the metadata to have the new shape (x and y and affine information)
            raster_meta.update({"driver": "GTiff",
                            "height": raster_crop.shape[1],
                            "width": raster_crop.shape[2],
                            "transform": raster_meta["transform"]})

    #         mask the nodata values
            raster_crop_ma = np.ma.masked_equal(raster_crop, 0) 
            
            
            for labels in gdf.loc[[index],"Labels"]:
                for label in labels:
                    
                    # output directory per label
                    output_label_dir = output_parent_dir + label
                    output_label_dir = self._add_trailing_slash(output_label_dir) 
                    
                    # create output directory if it doesn't exist
                    self._create_dir(output_label_dir)
                    

                    # output file path
                    outpath = output_label_dir + str(index+1) + '.tif'
                    print(outpath)

                    # Export cloud-masked TCI file
                    print(f'Cropping Polygon {index + 1} for Label "{label}"')
                    
                    with rio.open(outpath, 'w', **raster_meta) as outf:
                        outf.write(raster_crop_ma)


    def tif_to_jpg(self, in_dir, out_dir):
        in_dir_base = self._add_trailing_slash(in_dir)
        
        out_dir_base = self._add_trailing_slash(out_dir)
        
        # If the output parent folder doesn't exist, create it
        
        self._create_dir(out_dir)
        
        # List containing respective label directories
        
        in_dir_list = glob(in_dir_base + "*/")
        
        for in_dir_child in in_dir_list:
            
            print('in_dir_child:', in_dir_child)
            if self.windows:
                label = in_dir_child.split('\\')[-2]
            else:
                label = in_dir_child.split("/")[-2]
            
            # If output child folder doesn't exist, create
            
            out_dir_child = out_dir_base + label
            
            out_dir_child = self._add_trailing_slash(out_dir_child)
            
            self._create_dir(out_dir_child)
        

            # Export Polygons from TIF to  JPEG

            tif_list = glob(in_dir_child + "*.tif",recursive=True)

            for tif_path in tif_list:
                print('tif_path:', tif_path)
                if self.windows:
                    base_filename = tif_path.split('\\')[-1].split('.')[0]
                else:
                    base_filename = tif_path.split("/")[-1].split(".")[0]
                im = Image.open(tif_path)
                im.thumbnail(im.size)
                im.save(out_dir_child + base_filename + ".jpg", "JPEG", quality=100)



# if __name__ == "__main__":
#     cpipe = CollectionPipeline(shub_instance_id="dc0df77a-165f-4d52-b932-546f85d68353",bounding_box = (8.42823, -3.373256, 25.688438, 5.845887),search_interval = ('2019-01-01', '2020-12-31'),tile_list = ['33MVV','33NTE'],output_dir="/Volumes/Lacie/zhenyadata/Project_Canopy_Data/PC_Data/Sentinel_Data/Labelled/Tiles_v3/DELETE_TEST/OOP_TEST_SINGLE_TILE",num_layers=2,raw_dir_s3_dest="/Volumes/Lacie/zhenyadata/Project_Canopy_Data/PC_Data/Sentinel_Data/Labelled/Tiles_v3/DELETE_TEST/s3_mock/raw",chips_dir_s3_dest="/Volumes/Lacie/zhenyadata/Project_Canopy_Data/PC_Data/Sentinel_Data/Labelled/Tiles_v3/DELETE_TEST/s3_mock/chips")
#     cpipe.run(start_step="upload",end_step="upload")
