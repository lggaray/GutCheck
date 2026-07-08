#!/usr/bin/env bash
# Deploy the nutrition-tracker Lambda function.
# Builds lambda_deployment.zip from scratch (never appends to a stale zip),
# uploads it, waits for the update, then runs a smoke test.
set -euo pipefail

cd "$(dirname "$0")"

FUNCTION_NAME="nutrition-tracker"
REGION="ap-southeast-2"
ZIP_FILE="lambda_deployment.zip"
SMOKE_OUT="/tmp/lambda_smoke.json"

# 1. Always start from a clean zip — appending to a stale zip keeps deleted files alive.
echo "==> Removing stale ${ZIP_FILE} (if any)"
rm -f "${ZIP_FILE}"

# 2. Zip dependencies so packages land at the zip root (Lambda requirement).
echo "==> Zipping lambda_package/ dependencies"
(cd lambda_package && zip -r "../${ZIP_FILE}" . -q -x "*.pyc" -x "*__pycache__*")

# 3. Add source files at the zip root.
echo "==> Adding source files"
zip "${ZIP_FILE}" extract.py handler.py db.py models.py -q

# 4. Upload.
echo "==> Uploading to Lambda (${FUNCTION_NAME}, ${REGION})"
aws lambda update-function-code \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --zip-file "fileb://${ZIP_FILE}" \
  --query '{State:State,CodeSize:CodeSize}' --output json

# 5. Wait until the update is fully applied.
echo "==> Waiting for function update to complete"
aws lambda wait function-updated \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}"

# 6. Smoke test: invoke with an empty body and expect statusCode 200.
echo "==> Running smoke test"
aws lambda invoke \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --payload '{"body":"{}"}' \
  --cli-binary-format raw-in-base64-out \
  "${SMOKE_OUT}" > /dev/null

if grep -q '"statusCode": 200' "${SMOKE_OUT}"; then
  echo "Smoke test: PASS"
else
  echo "Smoke test: FAIL — response was:"
  cat "${SMOKE_OUT}"
  exit 1
fi

echo
echo "Deploy complete. Check logs with:"
echo "  aws logs tail /aws/lambda/${FUNCTION_NAME} --since 5m --format short"
