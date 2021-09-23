FROM httpd:2.4

MAINTAINER zrn-ns

# Install python and pip
RUN apt update -y
RUN apt install python3 -y -qq --no-install-recommends
RUN apt install python3-pip -y -qq --no-install-recommends
RUN apt install python3-setuptools -y -qq --no-install-recommends
RUN pip3 install --upgrade pip
RUN apt install vim -y -qq --no-install-recommends

# copy applications
COPY app/ /usr/src/app/

# install Python modules needed by the Python app
RUN pip install --no-cache-dir -r /usr/src/app/requirements.txt

# copy files required for the app to run
COPY htdocs /usr/local/apache2/htdocs

# tell the port number the container should expose
EXPOSE 80

CMD ["/usr/src/app/startup.sh"] 

