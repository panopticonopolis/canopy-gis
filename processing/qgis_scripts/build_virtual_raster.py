import glob
import subprocess


base = 'D:/canopy_data/pipeline-products/'

input_dir = base + 'virtual_rasters/SCL/'

output_dir = base + 'virtual_rasters/SCL/'

input_files = [input_dir + f'Layer{i}.vrt\n' for i in reversed(range(1,11))]

textfile = input_dir + 'names.txt'

with open(textfile, 'w') as nf:
    nf.writelines(input_files)
    
output_file = output_dir + 'full.vrt'

command = ['gdalbuildvrt', '-resolution', 'average', '-r', 'nearest', '-srcnodata', '0', '-input_file_list'] + [textfile] + [output_file]

subprocess.run(command)

#for i in range(1, 11):
#    print(f'Making raster for layer {i}')
#    
#    input_folder = [input_dir + f'{i}/*.jp2']
#    
#    output_file = [output_dir + f'Layer{i}.vrt']
#    
#    command = ['gdalbuildvrt', '-resolution', 'average', '-r', 'nearest', '-srcnodata', '0'] + output_file + input_folder
#    
#    subprocess.run(command)
    
print('Finished')