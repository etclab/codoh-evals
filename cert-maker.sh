sudo -v

CODOH_EVAL_DIR=$(pwd)/.signing
COREDNS_DIR=$(pwd)/../coredns/.signing

rm -rf $CODOH_EVAL_DIR
mkdir $CODOH_EVAL_DIR
cd $CODOH_EVAL_DIR

openssl genrsa -out local-ca.key 4096
openssl req -x509 -new -nodes -key local-ca.key -sha256 -days 3650 \
  -subj "/CN=Local CA" \
  -out local-ca.crt

sudo cp local-ca.crt /usr/local/share/ca-certificates/local-ca.crt
sudo update-ca-certificates

cat > target-san.cnf <<'EOF'
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = req_ext

[ dn ]
CN = 127.0.0.1

[ req_ext ]
subjectAltName = @alt_names

[ alt_names ]
IP.1  = 127.0.0.1
DNS.1 = localhost
EOF

cat > proxy-san.cnf <<'EOF'
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = req_ext

[ dn ]
CN = 127.0.0.1

[ req_ext ]
subjectAltName = @alt_names

[ alt_names ]
IP.1  = 127.0.0.1
DNS.1 = localhost
EOF

rm -rf $COREDNS_DIR
mkdir $COREDNS_DIR
cd $COREDNS_DIR

openssl genrsa -out target-key.pem 2048
openssl req -new -key target-key.pem -out target.csr -config $CODOH_EVAL_DIR/target-san.cnf

openssl x509 -req -in target.csr \
  -CA $CODOH_EVAL_DIR/local-ca.crt \
  -CAkey $CODOH_EVAL_DIR/local-ca.key -CAcreateserial \
  -out target.pem -days 825 -sha256 \
  -extensions req_ext -extfile $CODOH_EVAL_DIR/target-san.cnf

openssl genrsa -out proxy-key.pem 2048
openssl req -new -key proxy-key.pem -out proxy.csr -config $CODOH_EVAL_DIR/proxy-san.cnf

openssl x509 -req -in proxy.csr \
  -CA $CODOH_EVAL_DIR/local-ca.crt \
  -CAkey $CODOH_EVAL_DIR/local-ca.key -CAcreateserial \
  -out proxy.pem -days 825 -sha256 \
  -extensions req_ext -extfile $CODOH_EVAL_DIR/proxy-san.cnf

openssl x509 -in proxy.pem -outform der | openssl dgst -sha256 -binary | base64
