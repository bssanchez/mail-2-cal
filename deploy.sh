#!/usr/bin/env bash
# Despliega (o actualiza) mail2cal en AWS Lambda con ejecución programada.
#
#   ./deploy.sh            crea o actualiza todo y hace una ejecución de prueba
#   ./deploy.sh --destroy  borra todo lo que creó este script
#
# Requiere: AWS CLI v2 configurado (aws configure), python3 y zip. Usa uv si está instalado.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "Falta .env (copia .env.example y rellénalo)" >&2; exit 1; }
set -a; source ./.env; set +a

FN="${FUNCTION_NAME:-mail2cal}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
REGION="${REGION:-us-east-1}"
SCHEDULE="${SCHEDULE:-rate(10 minutes)}"
ROLE="${FN}-lambda-role"
RULE="${FN}-schedule"
PARAM_PREFIX="/${FN}"
SECRETS_PARAM="${PARAM_PREFIX}/secrets"
STATE_PARAM="${PARAM_PREFIX}/state"
RUNTIME="python3.13"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION" AWS_PAGER="" FUNCTION_NAME="$FN"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
TMP="$(mktemp -d)"; chmod 700 "$TMP"; trap 'rm -rf "$TMP"' EXIT
step() { printf '\n==> %s\n' "$*"; }

if [[ "${1:-}" == "--destroy" ]]; then
  step "Borrando $FN en $REGION (cuenta $ACCOUNT)"
  aws events remove-targets --rule "$RULE" --ids 1 >/dev/null 2>&1 || true
  aws events delete-rule --name "$RULE" 2>/dev/null || true
  aws lambda delete-function --function-name "$FN" 2>/dev/null || true
  aws iam delete-role-policy --role-name "$ROLE" --policy-name "${FN}-ssm" 2>/dev/null || true
  aws iam detach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE" 2>/dev/null || true
  aws ssm delete-parameters --names "$SECRETS_PARAM" "$STATE_PARAM" >/dev/null 2>&1 || true
  aws logs delete-log-group --log-group-name "/aws/lambda/$FN" 2>/dev/null || true
  echo "Listo."
  exit 0
fi

for v in IMAP_USER IMAP_PASSWORD GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_REFRESH_TOKEN; do
  [[ -n "${!v:-}" ]] || { echo "Falta $v en .env" >&2; exit 1; }
done

step "Desplegando $FN en $REGION (cuenta $ACCOUNT)"

step "Guardando credenciales cifradas en SSM ($SECRETS_PARAM)"
python3 -c 'import json, os; print(json.dumps({k: os.environ[k] for k in
  ("IMAP_PASSWORD", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")}))' > "$TMP/secrets.json"
aws ssm put-parameter --name "$SECRETS_PARAM" --type SecureString --overwrite \
  --value "file://$TMP/secrets.json" >/dev/null

step "Rol IAM ($ROLE)"
if ! ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text 2>/dev/null)"; then
  ROLE_ARN="$(aws iam create-role --role-name "$ROLE" --query Role.Arn --output text \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
      "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}')"
  NEW_ROLE=1
fi
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
cat > "$TMP/policy.json" <<EOF
{"Version": "2012-10-17", "Statement": [
  {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
   "Resource": "arn:aws:ssm:${REGION}:${ACCOUNT}:parameter${PARAM_PREFIX}/*"},
  {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": "*",
   "Condition": {"StringEquals": {"kms:ViaService": "ssm.${REGION}.amazonaws.com"}}}
]}
EOF
aws iam put-role-policy --role-name "$ROLE" --policy-name "${FN}-ssm" --policy-document "file://$TMP/policy.json"

step "Empaquetando código"
rm -rf build dist && mkdir -p build dist
if command -v uv >/dev/null; then
  uv pip install --quiet --target build --python-version 3.13 --python-platform aarch64-manylinux2014 \
    -r requirements.txt
else
  python3 -m pip install --quiet --target build --platform manylinux2014_aarch64 --implementation cp \
    --python-version 3.13 --only-binary=:all: -r requirements.txt
fi
cp -r mail2cal lambda_function.py build/
find build -name __pycache__ -prune -exec rm -rf {} +
(cd build && zip -qr ../dist/mail2cal.zip .)
echo "dist/mail2cal.zip: $(du -h dist/mail2cal.zip | cut -f1)"

python3 - "$TMP/env.json" <<'PY'
import json, os, sys
keys = ["IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_FOLDERS", "GOOGLE_CLIENT_ID", "GOOGLE_CALENDAR_ID",
        "DEFAULT_TIMEZONE", "BACKFILL_DAYS"]
env = {k: os.environ[k] for k in keys if os.environ.get(k)}
prefix = "/" + os.environ.get("FUNCTION_NAME", "mail2cal")
env.update(STATE_BACKEND="ssm", STATE_SSM_PARAM=f"{prefix}/state", SECRETS_SSM_PARAM=f"{prefix}/secrets")
json.dump({"Variables": env}, open(sys.argv[1], "w"))
PY

step "Función Lambda ($FN)"
if aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file fileb://dist/mail2cal.zip \
    --architectures arm64 >/dev/null
  aws lambda wait function-updated --function-name "$FN"
  aws lambda update-function-configuration --function-name "$FN" --runtime "$RUNTIME" \
    --handler lambda_function.handler --timeout 120 --memory-size 256 \
    --environment "file://$TMP/env.json" >/dev/null
else
  [[ -n "${NEW_ROLE:-}" ]] && { echo "Esperando a que el rol IAM se propague..."; sleep 10; }
  for attempt in 1 2 3 4 5 6; do
    if aws lambda create-function --function-name "$FN" --runtime "$RUNTIME" --architectures arm64 \
        --role "$ROLE_ARN" --handler lambda_function.handler --timeout 120 --memory-size 256 \
        --zip-file fileb://dist/mail2cal.zip --environment "file://$TMP/env.json" >/dev/null 2>"$TMP/err"; then
      break
    fi
    grep -q "cannot be assumed" "$TMP/err" && [[ $attempt -lt 6 ]] || { cat "$TMP/err" >&2; exit 1; }
    sleep 5
  done
fi
aws lambda wait function-updated --function-name "$FN"
# Si una pasada falla, la siguiente (10 min después) ya reintenta: sin reintentos extra.
aws lambda put-function-event-invoke-config --function-name "$FN" --maximum-retry-attempts 0 >/dev/null
FN_ARN="$(aws lambda get-function --function-name "$FN" --query Configuration.FunctionArn --output text)"

aws logs create-log-group --log-group-name "/aws/lambda/$FN" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "/aws/lambda/$FN" --retention-in-days 14

step "Programación: $SCHEDULE"
RULE_ARN="$(aws events put-rule --name "$RULE" --schedule-expression "$SCHEDULE" --query RuleArn --output text)"
aws lambda add-permission --function-name "$FN" --statement-id "$RULE" --action lambda:InvokeFunction \
  --principal events.amazonaws.com --source-arn "$RULE_ARN" >/dev/null 2>&1 || true
aws events put-targets --rule "$RULE" --targets "Id=1,Arn=$FN_ARN" >/dev/null

step "Ejecución de prueba"
aws lambda invoke --function-name "$FN" --log-type Tail "$TMP/out.json" \
  --query LogResult --output text | base64 -d | grep -v -E '^(START|END|REPORT) ' || true
echo "Resultado: $(cat "$TMP/out.json")"

cat <<EOF

Listo. mail2cal corre $SCHEDULE.
  Ver logs:        aws logs tail /aws/lambda/$FN --follow --region $REGION
  Ejecutar ya:     aws lambda invoke --function-name $FN --region $REGION /dev/stdout
  Desinstalar:     ./deploy.sh --destroy
EOF
