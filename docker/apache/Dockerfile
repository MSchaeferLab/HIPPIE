# Apache/2.4.66 (Debian) — Debian trixie ships apache2 2.4.66 in the
# default repos, so this image's Server header reads exactly that.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        apache2 \
        gettext-base \
 && rm -rf /var/lib/apt/lists/* \
 && a2enmod proxy proxy_http headers rewrite expires \
 && a2dissite 000-default \
 && ln -sf /dev/stdout /var/log/apache2/access.log \
 && ln -sf /dev/stderr /var/log/apache2/error.log \
 && echo "ServerName localhost" >> /etc/apache2/apache2.conf

COPY hippie.conf.template /etc/apache2/sites-available/hippie.conf.template
COPY entrypoint-apache.sh /entrypoint-apache.sh
RUN chmod +x /entrypoint-apache.sh

EXPOSE 80

ENTRYPOINT ["/entrypoint-apache.sh"]
CMD ["apache2ctl", "-D", "FOREGROUND"]
