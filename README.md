# HIPPIE_FACELIFT

Clone the repository:

```bash
git clone https://github.com/PelzKo/HIPPIE_FACELIFT.git
cd HIPPIE_FACELIFT
```

Create the virtual environment and install the dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

First migrate, run create superuser and then run the server:

```bash
cd hippie_django
python manage.py migrate

# If you want example data
python manage.py seed_test_data
python manage.py test_import_bait_prey
# If you want to import the real data
python manage.py import_hippie_sql data/mschaefer_hippie_v2_v2-4.sql --log-file data/import.log

python manage.py createsuperuser
npm run build
python manage.py runserver
```

When you change anything in the frontend, you need to run the following command to build the frontend:

```bash
npm run build
python manage.py collectstatic
```
