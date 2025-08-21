FROM python:3.10-alpine AS base

RUN apk --update add ffmpeg git

FROM base AS builder

WORKDIR /install
COPY requirements.txt /requirements.txt

RUN apk add gcc libc-dev zlib zlib-dev jpeg-dev
RUN pip install --prefix="/install" -r /requirements.txt

FROM base

COPY --from=builder /install /usr/local/lib/python3.10/site-packages
RUN mv /usr/local/lib/python3.10/site-packages/lib/python3.10/site-packages/* /usr/local/lib/python3.10/site-packages/

COPY zotify /app/zotify

WORKDIR /app
EXPOSE 4381
CMD ["python3", "-m", "zotify"]
