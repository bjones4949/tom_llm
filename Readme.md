## Installation
1. https://docs.docker.com/engine/install/ubuntu/
2. add $user to docker group
sudo usermod -aG docker $USER
newgrp docker
3. clone repo using: git clone https://github.com/bjones4949/local_llm.git\





## Onedrive setup
Then OneDrive sign-in and start-up:

bash
cd ~/local_llm
docker compose down
sudo mkdir -p /srv/onedrive && sudo chown $(id -u):$(id -g) /srv/onedrive
docker compose run --rm -it onedrive

Sign in, wait for “Sync with Microsoft OneDrive is complete”, press Ctrl+C, then:

bash
docker compose up -d
docker compose logs -f llm




## Start command 
docker compose up -d



Getting your OneDrive files into AnythingLLM. Right now the files are visible to the container at /app/onedrive, but AnythingLLM doesn't pick them up on its own. For now, upload documents into a workspace through the UI. When you want it automatic, the next step would be a small script that watches /srv/onedrive and pushes new or changed files through AnythingLLM's API, which could run as another service in this same compose file.

When the GPU arrives, the only service that changes is vllm. You swap build: ./vllm-cpu for the official vllm/vllm-openai image, add the GPU deploy: block, pick a bigger model, and raise the token limits in both services. Everything else stays as it is.