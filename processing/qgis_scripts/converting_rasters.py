import subprocess
import glob
import os

base = 'D:/canopy_data/pipeline-products/'

input_directory = base + 'cloudless_products/SCL/*/*.jp2'

TCI_files = glob.glob(input_directory, recursive=True)

output_directory = base + 'converted_products/SCL/'

total = len(TCI_files)
n = 1

for f in TCI_files:
    print(f'processing file {n} of {total}')
    n += 1
    folder_num = f.split('\\')[1]
    output_folder = output_directory + folder_num + '/'
    if not os.path.isdir(output_folder):
        os.mkdir(output_folder)
    filename = f.split('\\')[2]
    output_filepath = output_folder + filename
    #print('output path:', output_filepath)
    cmd = ['gdalwarp', '-t_srs', 'EPSG:4326', '-r', 'near', '-of', 'GTiff', f, output_filepath]
    subprocess.call(cmd, shell=True)

print('Finished')