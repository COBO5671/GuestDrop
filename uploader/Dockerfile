FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/uploader .

FROM scratch
COPY --from=build /out/uploader /uploader
USER 10001:999
EXPOSE 8090
ENTRYPOINT ["/uploader"]
