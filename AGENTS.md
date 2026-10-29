# AGENTS Instructions
- All development commands must run inside Docker containers defined in `docker-compose.yml`, local virtualenv does not exist and is not maintained.
- If Docker is not running, start the required services and continue. If you are not able to start Docker, stop and inform the user.
- README.md contains important information for Agents.
- For production deployment from this repo, wait for GitHub Actions on `main` to build and publish both images, then use `./scripts/deploy-prod.sh --tag sha-FULL_COMMIT_SHA` as the canonical deployment workflow (verify existing images, gitops update, helm upgrade, rollout checks). The script never builds or pushes images. Do not manually repeat deployment steps unless debugging the script.
