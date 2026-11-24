# sim-viz static front end (Phaser 3, vendored) served by unprivileged nginx on 8080.
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html style.css /usr/share/nginx/html/
COPY src /usr/share/nginx/html/src
COPY vendor /usr/share/nginx/html/vendor
EXPOSE 8080
