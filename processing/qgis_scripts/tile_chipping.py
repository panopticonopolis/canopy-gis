import os, subprocess,time,gdal

 
gdal.UseExceptions()

print(time.strftime("%a, %d %b %Y %H:%M:%S +0000")) 
 
in_path = "/Volumes/Lacie/zhenyadata/Project_Canopy_Data/PC_Data/Sentinel_Data/Labelled/Tiles_v2_Misha/Polygon_Crops/Individual_Polygons/"
input_filename = '0.tif'
 
out_path = '/Volumes/Lacie/zhenyadata/Project_Canopy_Data/PC_Data/Sentinel_Data/Labelled/Tiles_v2_Misha/Polygon_Crops/Chips/0/'
output_filename = 'tile_'
 
tile_size_x = 100
tile_size_y = 100
 
ds = gdal.Open(in_path + input_filename)
band = ds.GetRasterBand(1)
xsize = band.XSize
ysize = band.YSize
 
for i in range(0, xsize, tile_size_x):
    for j in range(0, ysize, tile_size_y):
        com_string = "gdal_translate -of GTiff -srcwin " + str(i)+ ", " + str(j) + ", " + str(tile_size_x) + ", " + str(tile_size_y) + " " + str(in_path) + str(input_filename) + " " + str(out_path) + str(output_filename) + str(i) + "_" + str(j) + ".tif"
        subprocess.call(com_string,shell=True)