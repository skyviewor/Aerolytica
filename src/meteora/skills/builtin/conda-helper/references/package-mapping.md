# CLI Tool → conda Package Mapping

When a shell command returns `command not found`, use this table to find the conda package.

| Missing Command | conda Package | Channel | Purpose |
|----------------|---------------|---------|---------|
| ncks | nco | conda-forge | NetCDF hyperslab extraction |
| ncrcat | nco | conda-forge | NetCDF file concatenation |
| ncap2 | nco | conda-forge | NetCDF arithmetic operations |
| ncatted | nco | conda-forge | NetCDF attribute editing |
| ncra | nco | conda-forge | NetCDF time averaging |
| ncea | nco | conda-forge | NetCDF ensemble averaging |
| ncdiff | nco | conda-forge | NetCDF differencing |
| ncflint | nco | conda-forge | NetCDF linear interpolation |
| ncpdq | nco | conda-forge | NetCDF dimension reordering |
| ncwa | nco | conda-forge | NetCDF weighted averaging |
| ncremap | nco | conda-forge | NetCDF regridding |
| cdo | cdo | conda-forge | Climate data operators |
| grib_ls | eccodes | conda-forge | GRIB file listing |
| grib_dump | eccodes | conda-forge | GRIB file dump |
| grib_copy | eccodes | conda-forge | GRIB file copy/extract |
| grib_filter | eccodes | conda-forge | GRIB filter rules |
| gdal_translate | gdal | conda-forge | Raster format conversion |
| gdalwarp | gdal | conda-forge | Raster reprojection |
| gdalinfo | gdal | conda-forge | Raster metadata query |
| ogr2ogr | gdal | conda-forge | Vector format conversion |
| ncdump | libnetcdf | conda-forge | NetCDF content dump |
| ncgen | libnetcdf | conda-forge | NetCDF file generation |

## Reverse Lookup

If the command is not in the table:

```bash
conda search <command_name> -c conda-forge 2>/dev/null | grep -v "^#" | head -10
```

Or search for packages containing the command:

```bash
conda search "*" -c conda-forge --info 2>/dev/null | grep -B5 "<command_name>" | head -20
```
