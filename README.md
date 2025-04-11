# Welcome to ChatIPT ![logosmall](https://github.com/user-attachments/assets/9aded457-b39e-41dc-bad6-92a9258ac563)



ChatIPT is a chatbot for students and researchers who are new to data publication or only occasionally publish data.

It cleans and standardises spreadsheets, creates basic metadata, asking the user for guidance where necessary through natural conversation. Finally, it publishes the data on gbif.org as a Darwin Core Archive.

> If you would like access to the testing version, please contact me rukayasj@uio.no.

* * *

**Why is it necessary?**

At a conservative estimate, there are 300 - 400 PhDs and MScs in Europe alone at the moment generating biodiversity data, as well as countless small biodiversity research studies.

Publishing piecemeal but high quality data such as this is difficult to do at scale:

*   🤔 Data standardisation is hard and requires specialist knowledge of:
    *   data standards and the the domain of standardisation (e.g. ontologies, etc)
    *   programming languages (e.g. R, Python, SQL)
    *   data management techniques (e.g. normalisation, wide vs long format, etc)
    *   familiarity with specialised software (e.g. OpenRefine and the IPT, etc)
*   📨 No open access to publishing facilities - users have to know who to email and have to wait to get added to IPTs manually
*   🕐 GBIF node staff time and resources are limited
*   🧑‍🎓 Training workshops can help, but:
    *   are costly and time consuming
    *   teach generic techniques which users find difficult to put into practice in the real world
    *   have logistical and language barriers
    *   have to be done regularly: users who only publish data once or twice a year forget how to do it and need the same help every time

ChatIPT solves these problems: a non-technical user without training or specialist knowledge only needs a web browser and verified ORCID account to go from an unformatted, raw spreadsheet to standardised, published data on GBIF.

* * *

**Future plans**

1.  Restrict access with an ORCID login
2.  Build in strict safety rails to ensure the bot is only used for legitimate data publication
3.  Create a front page dashboard listing a logged-in user's datasets, along with some stats for each dataset from the GBIF API
4.  Provide edit access for already published or work-in-progress datasets
5.  Currently publishing using the GBIF Norway publishing institution - this would need to be opened up to more countries. National nodes would sign up for it (agreeing that ad-hoc users can publish to a generic national institution), and publicise it at their higher education institutions.
6.  Only works well at the moment for occurrence data - expand to sampling event, checklist and others.
7.  Add support for frictionless data & the new data models
8.  Test chatbot thoroughly in other languages
9.  Parse PDF uploads (e.g. drafts of journal papers) to create better metadata for each dataset
10.  Use the details from the user's ORCID login to give chatbot context so it can provide more tailored help. For example, it could read biographies to discover user's area of expertise and make inferences about the data from that, automatically get current institution name + address for metadata, work out likely level of experience with data publication and tailor language accordingly, and more. The chatbot could also be more personalised and human-like, addressing the user by name, commenting on the new dataset compared to the old work done previously, etc.
11.  Currently using OpenAI's gpt4o model - experiment with open source models to reduce costs, depending on uptake
* * *
Note: Not suitable for publishing data from a database, or for large data sources, and there are no plans to support this. A chatbot is not the right format as it needs to be done by a technician who understands the database, and as there are far fewer databases than ad-hoc spreadsheets it is (in many ways) a different problem, which we already have a great tool for: the IPT. The IPT is less good for those new to data publication who only need to publish a small, single datase once or twice every few years.

* * *
## How it works - technical details

There is a React front end handling the chat interface and displaying the dataframes, and a Python (Django) API which interacts with the OpenAI API for GPT4o. The model is given a series of prompts that it runs through in order to standardise the data - this works better (so far) than trying to do it in single prompt. The generic prompt template is here: https://github.com/gbif-norway/publishgpt/blob/main/back-end/api/templates/prompt.txt, and the specific tasks the model runs through are here: https://github.com/gbif-norway/publishgpt/blob/main/back-end/api/fixtures/tasks.yaml

The data is stored in a Postgres database, with different models for Datasets, Tables (dataframes), Tasks and the Agent/Messages conversation system needed for managing the Tasks.

GPT4o is given access to a number of tools/functions which run on the server side, which it can call on to perform tasks in the Django environment. The most important of these is the Python tool which allows it to run any Python code to edit the dataset and dataframes, with certain constraints.

Another important tool is the Publish tool, which the model is instructed to use in its final Task in order to publish data to the GBIF test portal. It creates a Darwin Core Archive using https://github.com/pieterprovoost/dwca-writer, uploads it to a public repository and uses the GBIF API to register it as a dataset with GBIF.

# Using DevSpace with ChatIPT

This project uses DevSpace for development and deployment. Here's how to use it:

## Setup

1. Install DevSpace: Follow the instructions at [https://devspace.sh/cli/docs/getting-started/installation](https://devspace.sh/cli/docs/getting-started/installation)
2. Ensure you have access to a Kubernetes cluster and `kubectl` is configured correctly

Set up namespace with:
```bash
devspace use namespace publishbot
```

## Development

### Backend Development

To start developing the backend:

```bash
devspace dev
```

This command will:
1. Deploy any dependencies
2. Ensure pull secrets
3. Deploy Helm charts and manifests
4. Start the backend in development mode
   - Sync files between your local `./back-end` directory and the container
   - Open a terminal in the container running `./devspace_start.sh`
   - Enable SSH for IDE connections
   - Forward port 8000
   - Open `http://localhost:8000` when available

### Frontend Development

To start developing the frontend:

```bash
devspace run-pipeline dev-fe
```

This command is similar to backend development but focuses on the frontend:
- Syncs files from `./front-end`
- Forwards port 3000
- Opens `http://localhost:3000` when available

## Deployment

To build and deploy the entire application:

```bash
devspace deploy
```

This will:
1. Deploy dependencies
2. Ensure pull secrets
3. Build and push Docker images with tags based on the git commit hash
4. Deploy Helm charts and manifests

To deploy without rebuilding images:

```bash
devspace run-pipeline deploy-only
```

## Images

The project defines two images:
- `back-end`: Built from `./back-end/Dockerfile`
- `front-end`: Built from `./front-end/Dockerfile`

Both use Kaniko for building, with caching enabled.

## Helm Deployment

The project uses a Helm chart located at `./helm/publishgpt` for deployment.


## Notes

- The configuration uses git commit hashes for image tagging.
- Development modes inject a lightweight SSH server for IDE connections.
- Proxy commands are set up to make `devspace`, `kubectl`, `helm`, and git credentials available in dev containers.

For more detailed information about DevSpace and its capabilities, refer to the [DevSpace documentation](https://devspace.sh/cli/docs/introduction).
