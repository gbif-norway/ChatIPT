# Run server
`docker compose up` add --build and -d if you want
`docker exec -it chatipt-back-end-1 /bin/bash`
`./manage.py runserver 0.0.0.0:8000`
Check http://localhost:8000/api

# Load tasks
`./manage.py loaddata tasks`

# Wipe database and make migrations
`docker rm -v chatipt-db-1`, then start up the container again and don't forget
`./manage.py makemigrations && ./manage.py migrate && ./manage.py loaddata tasks`

# Run frontend
`docker exec -it chatipt-front-end-1 /bin/bash`
`npm run dev`
Check http://localhost:3000