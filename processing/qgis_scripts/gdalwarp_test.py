import os
import glob
from shutil import copyfile
import subprocess

input_directory = 'D:/canopy_data/L2A/republic-of-the-congo/2020_full_reprojected/'

filenames = glob.glob(input_directory + '*/T33MWR*', recursive=True)

dest_folder = 'D:/canopy_data/L2A/test/'

for f in filenames:
    folder_num = f.split('\\')[1]
    name = f.split('\\')[2]
    copyfile(f, dest_folder + folder_num + '_' + name)

command = ['gdalwarp', '--config', 'GDAL_CACHEMAX', '2000', '-wm', '2000', '-multi', '-overwrite', '-srcnodata', '0', '-dstnodata', '0']

input_directory_2 = 'D:/canopy_data/L2A/test/'

filenames2 = glob.glob(input_directory_2 + '*.jp2', recursive=True)

print(filenames2)

command += filenames2

print(command)

subprocess.run(command)

print('Finished')