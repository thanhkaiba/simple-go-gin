FROM golang:alpine

MAINTAINER Maintainer

WORKDIR /go/src/decode

ENV PORT=8080

COPY main.go .
COPY go.mod .
COPY vendor ./vendor

RUN go build -o app

EXPOSE $PORT

ENTRYPOINT ["./app"]