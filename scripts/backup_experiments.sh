#! /bin/bash
# from troodon:/mnt/data/experiments_backup
unalias rsync &> /dev/null

echo -e "\n----------------\n"
date
echo

echo -e "\ncluster jobs"
rsync -rzt --ignore-existing --stats festi100@hpc.rz.uni-duesseldorf.de:/home/festi100/git/HelixerPrep/jobs cluster_jobs/
