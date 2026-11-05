# MozTools Documents Engine

A first, self-hosted document-conversion API using LibreOffice.

## Endpoints

- `GET /health` — service health check
- `GET /formats` — advertised output extensions
- `POST /convert` — multipart upload with fields:
  - `file`: the document
  - `output_format`: for example `pdf`, `docx`, `xlsx`, `csv`, `pptx`, `txt`

## Deploy on Render

1. Create a new GitHub repository named `moztools-own-engine`.
2. Upload all files in this folder to the repository root.
3. In Render, choose **New + → Web Service**, connect the repository,
   and select Docker runtime (or use the included `render.yaml` blueprint).
4. Wait for deployment.
5. Test `https://YOUR-SERVICE.onrender.com/health`.

## Important limitations

- This is a starter engine, not a promise that every pair of formats works.
  LibreOffice decides which individual conversions are supported.
- The free Render instance may sleep, have limited RAM/CPU, and cannot provide
  unlimited public conversions. Heavy public traffic needs suitable hosting.
- Add authentication, rate limiting, request-size controls, malware scanning,
  temporary-file cleanup, and isolated worker execution before opening this
  API to unrestricted public traffic.
- This first version handles document/office formats only. Image, video and
  audio engines will be separate additions after document tests pass.
