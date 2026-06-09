#!/bin/bash

wget -N https://fms.alliancegenome.org/download/ORTHOLOGY-ALLIANCE_COMBINED.tsv.gz && gunzip ORTHOLOGY-ALLIANCE_COMBINED.tsv.gz 

while IFS=$'\t' read -r taxon species file; do
    echo "Downloading $species ($taxon): $file"
    wget -N "https://ftp.ebi.ac.uk/pub/databases/intact/current/psimitab/species/$file" && unzip -o "$file"
 
done < species_filename_mapping.tsv

