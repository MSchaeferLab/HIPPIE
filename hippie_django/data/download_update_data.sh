#!/usr/bin/env bash
set -euo pipefail

# Always download into the script's own directory so this works from any CWD
# (local: cd hippie_django && sh data/download_update_data.sh,
#  Docker: docker compose exec web sh data/download_update_data.sh)
cd "$(dirname "$0")"

curl -fsSL -O -z sec_ac.txt "https://ftp.uniprot.org/pub/databases/uniprot/knowledgebase/complete/docs/sec_ac.txt"
curl -fsSL -O -z HUMAN_9606_idmapping.dat.gz "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/idmapping/by_organism/HUMAN_9606_idmapping.dat.gz" && gunzip -f HUMAN_9606_idmapping.dat.gz
curl -fsSL -O -z Homo_sapiens.gene_info.gz "https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/Homo_sapiens.gene_info.gz" && gunzip -f Homo_sapiens.gene_info.gz
curl -fsSL -O -z human.zip "https://ftp.ebi.ac.uk/pub/databases/intact/current/psimitab/species/human.zip" && unzip -o human.zip && rm human.zip
curl -fsSL -O -z BIOGRID-ALL-LATEST.mitab.zip "https://downloads.thebiogrid.org/Download/BioGRID/Latest-Release/BIOGRID-ALL-LATEST.mitab.zip" && unzip -o BIOGRID-ALL-LATEST.mitab.zip && rm BIOGRID-ALL-LATEST.mitab.zip
curl -fsSL -O -z GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt "https://storage.googleapis.com/adult-gtex/annotations/v11/metadata-files/GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt"
curl -fsSL -O -z GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct.gz "https://storage.googleapis.com/adult-gtex/bulk-gex/v11/rna-seq/GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct.gz" && gunzip -f GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_reads.gct.gz
