# TLS Material

Production-like compose expects the following mounted secret files:

- `tls_cert.pem`
- `tls_key.pem`

Development compose disables TLS and uses plain HTTP through the reverse proxy.

