sudo apt-get install -y postgresql postgresql-contrib  
sudo -u postgres psql -c "CREATE USER hippie WITH PASSWORD 'hippie';"
sudo -u postgres psql -c "CREATE DATABASE hippie OWNER hippie;"
