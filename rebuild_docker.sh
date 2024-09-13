# secret=$(python run_page/get_garmin_secret.py $(sed -n 1p secret.txt) $(sed -n 2p secret.txt) --is-cn)
secret=$(< secret_string.txt)
docker build -t running_page:latest . --build-arg app=Garmin-CN --build-arg secret_string=$secret
docker stop running
docker rm running
docker run -itd --restart=unless-stopped -p 42195:80 --name running  running_page:latest
