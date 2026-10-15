# Setup 
git pull
cp .env.example .env
sed -i "s/^ANYTHINGLLM_JWT_SECRET=.*/ANYTHINGLLM_JWT_SECRET=$(openssl rand -hex 32)/" .env
sed -i "s/^VLLM_API_KEY=.*/VLLM_API_KEY=$(openssl rand -hex 32)/" .env
chmod 600 .env
docker compose up -d anythingllm


# Setup sharepoint
mkdir -p sync/certs && cd sync/certs
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 730 -nodes -subj "/CN=spark-sync-test"
chmod 600 key.pem && sudo chown 1000:1000 key.pem cert.pem && cd ../..


# Entra admin:

Go to Identity > Applications > App registrations > New registration.
single tenant

Application (client) ID, which goes in SP_CLIENT_ID
Directory (tenant) ID, which goes in SP_TENANT_ID

certificates and secrets
save cert.pem and upload
Add permission SItes.Selected
grant
can remove user.read

# graph explorer
https://developer.microsoft.com/graph/graph-explorer
run query: GET https://graph.microsoft.com/v1.0/sites/yourtenant.sharepoint.com:/sites/CaseFilesTest?$select=id

POST https://graph.microsoft.com/v1.0/sites/commercialitsa.sharepoint.com,7fa9cb3d-e36c-4c9a-b32b-618d13f2629c,240bfd2e-4d7c-4649-9de5-c895f3847432/permissions
{
  "roles": ["write"],
  "grantedToIdentities": [
    { "application": { "id": "1fbc710c-6b56-4e68-8f79-7ce07407ebba", "displayName": "Spark Sync (test)" } }
  ]
}


set 	Require documents to be checked out before they can be edited?
 	 Yes   	 No to no