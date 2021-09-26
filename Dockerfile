FROM httpd:2.4

MAINTAINER zrn-ns

# アプリのルートURLを引数として受け取る
ARG APP_ROOT_URL="http://localhost:80/"
ENV APP_ROOT_URL=$APP_ROOT_URL

# Install python and pip
RUN apt-get update
RUN apt-get install python3 python3-pip vim python3-setuptools -y -qq --no-install-recommends
RUN pip3 install --upgrade pip

# copy applications
COPY app/ /usr/src/app/

# install Python modules needed by the Python app
RUN pip install --no-cache-dir -r /usr/src/app/requirements.txt

# copy files required for the app to run
COPY htdocs /usr/local/apache2/htdocs

# tell the port number the container should expose
EXPOSE 80

CMD ["/usr/src/app/startup.sh"]
