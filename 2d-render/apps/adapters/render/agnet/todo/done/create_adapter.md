create agnet adapter here apps/agnet_adapter.
we don't change another repos but gather info from there.

1) CRITICAL read first docs/adapter_new.md
last done adapter structure you could see here  /home/igor/repos/infinitetalk/apps/finik_adapter (it's wrapper pattern, but for agnet we need bridge)

2) agnet adapter acts like a bridge between worker and agnet api here /home/igor/repos/2d-render/docs/api.md . so we don't change agnet repo /home/igor/repos/2d-render . we need just call to api and save result like in finik adapter to temporary path (last 10 jobs).

3) logic in adapter the same like in this script /home/igor/repos/2d-render/infra/local/test/api_test.sh - we call api with image and audio and then save images with audion in one mp4 file.

4) agnet adapter like finik adapter must have preprocessing picture
Image Size Requirements section here /home/igor/repos/2d-render/docs/input.md .
image preprocessor should be copied from /home/igor/repos/infinitetalk/apps/common/image_preprocessor.py to apps/common/adapter for reusing future adapters. but for the agnet we need extend preprocessor with size image check and converting to jpg flag by default true as in the /home/igor/repos/2d-render/docs/input.md. also better ensure on our side and do auto resize to max 1920px. see more info in the mentioned doc for better prepocessing (logs and comments in code are critical)

so critical rule - if we could reuse some with future adapters - place to the common adapter apps/common/adapter
so in fact adapter should reuse as much as possible from here apps/common/adapter

5) agnet worker already in our local and prod cicd infra/docker-compose.yml
and infra/docker-compose-prod.yml
and after implementation we add new service adapter-agnet
here .gitlab-ci.yml will be stop-agnet, build-agnet, deploy agnet that will do relevant logic for whole pair worker agnet and adapter agnet.

for local deployment should do the same make commands make stop agnet, make build agnet, make deploy agnet.
CRITICAL! don't reinvent wheel - see how already run other pair omni for eg

6) very observable logs and comments in code.


ps: 
so point the same worker exist and no need to change, it's handled network layer for queues, and adapter give opportunity to use neuro render, in this concrete case as bridge.