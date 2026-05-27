#!/usr/bin/env bash
set -euo pipefail

# Always download into the script's own directory so this works from any CWD
# (local: cd hippie_django && sh data/download_update_data.sh,
#  Docker: docker compose exec web sh data/download_update_data.sh)
cd "$(dirname "$0")"

wget -N "https://ftp.uniprot.org/pub/databases/uniprot/knowledgebase/complete/docs/sec_ac.txt"
wget -N "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/idmapping/by_organism/HUMAN_9606_idmapping.dat.gz" && gunzip -f HUMAN_9606_idmapping.dat.gz
wget -N "https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/Homo_sapiens.gene_info.gz" && gunzip -f Homo_sapiens.gene_info.gz
wget -N "https://ftp.ebi.ac.uk/pub/databases/intact/current/psimitab/species/human.zip" && unzip human.zip
wget -N "https://downloads.thebiogrid.org/Download/BioGRID/Latest-Release/BIOGRID-ALL-LATEST.mitab.zip" && unzip BIOGRID-ALL-LATEST.mitab.zip
wget -N "https://storage.googleapis.com/adult-gtex/annotations/v11/metadata-files/GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt"
wget -N "https://storage.googleapis.com/adult-gtex/bulk-gex/v11/rna-seq/GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct.gz" && gunzip -f GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct.gz
