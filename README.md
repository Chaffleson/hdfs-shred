# hdfs-shred
Client extension to ensure shredding of files deleted from HDFS.  

## Features

* [TODO]Managed via central config file.  
* [TODO]Logs all activity to HDFS location.  
* [TODO]Uses ZooKeeper to track state of deletion and shredding.  
* [TODO]Uses hadoop fsck to get file blocks. 
* [TODO]Uses HDFS Client to delete files.  
* [TODO]Uses Cron to schedule shred jobs on Datanodes to avoid IO conflict.  
* [TODO]Uses Linux shred command to destroy blocks as regular job.  

## Logic

### Structure

app root:   hdfs://apps/shred  
conf:       hdfs://apps/shred/hdfs-shred.conf  
log root:   hdfs://app-logs/shred/  
nn log:     hdfs://app-logs/shred/nn-clientxxx.log  
dn logs:    hdfs://app-logs/shred/dn-workerxxx.log  


### nn-client, for user interaction

Get block list for a file using hdfs fsck  
Write Block ID into ZooKeeper: /shred/-IP-blkinfo-  
Delete File in HDFS, skipping trash  
Update ZK as ready for shred

### dn-worker, set in cron job on all DataNodes

Get target blocks from ZK  
Read location of block based upon their IP  
Find block location on linux FS, using find  
CP Block to a folder /shred on same mountpoint   
Update ZK  
Use shred to get rid of it  
Remove CP from /shred  
Update ZK
