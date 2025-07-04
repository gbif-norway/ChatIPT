// This is a parameterized Jenkinsfile that builds Docker images and updates GitOps configuration
// for staging and production environments. The pipeline allows manual selection of the target environment.
//
// JENKINS CONFIGURATION:
// 1. Create a Pipeline project (not multibranch)
// 2. In Pipeline → Definition → Pipeline script from SCM:
//    - SCM: Git
//    - Repository URL: your-repo-url
//    - Credentials: your-git-credentials
//    - Script Path: Jenkinsfile
// 3. The pipeline will prompt for branch selection (staging/main) when triggered
// 4. This builds both backend and frontend images, then updates GitOps configuration

pipeline {
    agent {
        kubernetes {
            yaml '''
                apiVersion: v1
                kind: Pod
                spec:
                  containers:
                  - name: kaniko
                    image: gcr.io/kaniko-project/executor:debug
                    command:
                    - /busybox/cat
                    tty: true
                    volumeMounts:
                    - name: kaniko-secret
                      mountPath: /kaniko/.docker
                  volumes:
                  - name: kaniko-secret
                    secret:
                      secretName: kaniko-secret
                      items:
                      - key: .dockerconfigjson
                        path: config.json
            '''
        }
    }

    parameters {
        choice(
            name: 'BRANCH',
            choices: ['staging', 'main'],
            description: 'Select the branch/environment to build for'
        )
    }

    environment {
        REGISTRY = 'gbifnorway'
        BACKEND_IMAGE = 'publishgpt-back-end'
        FRONTEND_IMAGE = 'publishgpt-front-end'
        BRANCH_NAME = "${params.BRANCH}"
        ENVIRONMENT = "${env.BRANCH_NAME}"
    }

    stages {
        stage('Get App Version') {
            steps {
                script {
                    // Clone the GitOps repo to get the current appVersion
                    sh '''
                        rm -rf gitops-tmp
                        git clone https://github.com/gbif-norway/gitops.git gitops-tmp
                    '''
                    
                    // Read the appVersion from Chart.yaml
                    sh '''
                        cd gitops-tmp/apps/publishgpt
                        if ! command -v yq &> /dev/null; then
                            curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o yq
                            chmod +x yq
                            YQ=./yq
                        else
                            YQ=$(command -v yq)
                        fi
                        $YQ --version
                        APP_VERSION=$($YQ e '.appVersion' Chart.yaml)
                        echo "Current appVersion from Chart.yaml: $APP_VERSION"
                        echo "APP_VERSION=$APP_VERSION" > app_version.env
                    '''
                    
                    // Load the appVersion into environment
                    load 'app_version.env'
                    env.IMAGE_TAG = "${APP_VERSION}-${env.BUILD_NUMBER}"
                    echo "Generated image tag: ${env.IMAGE_TAG}"
                }
            }
        }

        stage('Build Backend') {
            steps {
                dir('back-end') {
                    container('kaniko') {
                        sh """
                            /kaniko/executor \
                                --context . \
                                --dockerfile Dockerfile \
                                --destination ${REGISTRY}/${BACKEND_IMAGE}:${IMAGE_TAG} \
                                --cache=true
                        """
                    }
                }
            }
        }

        stage('Build Frontend') {
            steps {
                dir('front-end') {
                    container('kaniko') {
                        sh """
                            /kaniko/executor \
                                --context . \
                                --dockerfile Dockerfile \
                                --destination ${REGISTRY}/${FRONTEND_IMAGE}:${IMAGE_TAG} \
                                --cache=true
                        """
                    }
                }
            }
        }

        stage('Update GitOps Repo') {
            steps {
                script {
                    // Clone the GitOps repo
                    sh '''
                        rm -rf gitops-tmp
                        git clone https://github.com/gbif-norway/gitops.git gitops-tmp
                    '''
                    
                    if (env.BRANCH_NAME == 'staging') {
                        // Update image tags in values-staging.yaml for staging
                        sh '''
                            cd gitops-tmp/apps/publishgpt
                            if ! command -v yq &> /dev/null; then
                                curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o yq
                                chmod +x yq
                                YQ=./yq
                            else
                                YQ=$(command -v yq)
                            fi
                            $YQ --version
                            echo "Updating image tags in values-staging.yaml for ${BRANCH_NAME}.${BUILD_NUMBER}"
                            $YQ e -i ".backEnd.image.tag = \"${BRANCH_NAME}-${BUILD_NUMBER}\"" values-staging.yaml
                            $YQ e -i ".frontEnd.image.tag = \"${BRANCH_NAME}-${BUILD_NUMBER}\"" values-staging.yaml
                        '''
                        // Commit and push changes
                        sh '''
                            cd gitops-tmp
                            git config user.email "ci-bot@gbif.no"
                            git config user.name "GBIF Jenkins CI"
                            git add apps/chatipt/values-staging.yaml
                            git commit -m "ci: update image tags in values-staging.yaml for ${BRANCH_NAME}.${BUILD_NUMBER} [skip ci]" || true
                            git push origin main
                        '''
                    } else if (env.BRANCH_NAME == 'main') {
                        // For production, increment the chart version and keep appVersion as the base
                        sh '''
                            cd gitops-tmp/apps/chatipt
                            if ! command -v yq &> /dev/null; then
                                curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o yq
                                chmod +x yq
                                YQ=./yq
                            else
                                YQ=$(command -v yq)
                            fi
                            $YQ --version
                            currentVersion=$($YQ e '.version' Chart.yaml)
                            appVersion=$($YQ e '.appVersion' Chart.yaml)
                            echo "Current version: ${currentVersion}"
                            echo "Current appVersion: ${appVersion}"
                            
                            # Increment the patch version for production release
                            newVersion=$(echo $currentVersion | awk -F. '{$NF = $NF + 1;} 1' | sed 's/ /./g')
                            echo "New version: ${newVersion}"
                            
                            $YQ e -i ".version = \"${newVersion}\"" Chart.yaml
                            # Keep appVersion as is - it represents the application version
                        '''
                        // Commit and push changes
                        sh '''
                            cd gitops-tmp
                            git config user.email "ci-bot@gbif.no"
                            git config user.name "GBIF Jenkins CI"
                            git add apps/chatipt/Chart.yaml
                            git commit -m "ci: increment chart version to ${newVersion} for production release [skip ci]" || true
                            git push origin main
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            deleteDir()
        }
        success {
            echo "🎉 Pipeline completed successfully for branch: ${env.BRANCH_NAME}"
            echo "📦 Images pushed to registry with tag: ${IMAGE_TAG}"
        }
        failure {
            echo "❌ Pipeline failed for branch: ${env.BRANCH_NAME}"
        }
    }
} 