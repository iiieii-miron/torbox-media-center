FROM ghcr.io/torbox-app/torbox-media-center:main

WORKDIR /app
COPY . .

CMD ["python", "main.py"]
