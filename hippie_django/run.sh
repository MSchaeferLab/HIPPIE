#!/bin/bash
celery -A hippie worker -l info 2>&1 > celery.log & 
python manager.py runserver
