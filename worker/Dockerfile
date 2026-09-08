FROM qgis/qgis:3.40.6

RUN apt-get update && \
    apt-get install -y python3-venv python3-pip

RUN python3 -m venv /opt/venv  && \
    /opt/venv/bin/pip install --upgrade pip  && \
    /opt/venv/bin/pip install fastapi uvicorn

ENV QGIS_PREFIX_PATH=/usr
ENV XDG_RUNTIME_DIR=/tmp/runtime-root
ENV QT_QPA_PLATFORM=offscreen
ENV PYTHONPATH=/usr/share/qgis/python

COPY app.py /app.py

CMD ["/opt/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]