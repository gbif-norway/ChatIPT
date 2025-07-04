// This is a dispatcher Jenkinsfile that only builds for staging and production branches
// Other branches will be skipped entirely.
//
// JENKINS CONFIGURATION:
// 1. Create a Multibranch Pipeline project
// 2. In Branch Sources → Add source → Git:
//    - Repository URL: your-repo-url
//    - Credentials: your-git-credentials
// 3. In Behaviors → Add → Filter by name (with wildcards):
//    - Include: staging, main, master
//    - Exclude: (leave empty or add patterns like dependabot/*)
// 4. This will only create pipeline jobs for staging and main/master branches
// 5. Other branches will be ignored entirely

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
        IMAGE_TAG = "${env.BRANCH_NAME}-${env.BUILD_NUMBER}"
        ENVIRONMENT = "${env.BRANCH_NAME}"
    }

    stages {
        stage('Set Chart Version') {
            steps {
                script {
                    dir('GitOps-infrastucture/apps/publishgpt') {
                        sh '''
                            if ! command -v yq &> /dev/null; then
                                curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o yq
                                chmod +x yq
                                YQ=./yq
                            else
                                YQ=$(command -v yq)
                            fi
                            $YQ --version
                            baseVersion=$($YQ e '.version' Chart.yaml | sed 's/-rc.*//')
                            newVersion="${baseVersion}-${BRANCH_NAME}.${BUILD_NUMBER}"
                            echo "Setting Chart version to: ${newVersion}"
                            $YQ e -i ".version = \"${newVersion}\"" Chart.yaml
                            $YQ e -i ".appVersion = \"${BUILD_NUMBER}\"" Chart.yaml
                        '''
                    }
                }
            }
        }

        stage('Setup Docker Buildx') {
            steps {
                script {
                    sh 'docker buildx version'
                    sh '''
                        if ! docker buildx inspect multiarch-builder >/dev/null 2>&1; then
                            docker buildx create --name multiarch-builder --use
                        else
                            docker buildx use multiarch-builder
                        fi
                        docker buildx inspect --bootstrap
                    '''
                }
            }
        }

        stage('Build Backend') {
            steps {
                dir('back-end') {
                    sh """
                        docker buildx build \
                            --platform linux/amd64 \
                            -t ${REGISTRY}/${BACKEND_IMAGE}:${IMAGE_TAG} \
                            --push .
                    """
                }
            }
        }

        stage('Build Frontend') {
            steps {
                dir('front-end') {
                    sh """
                        docker buildx build \
                            --platform linux/amd64 \
                            -t ${REGISTRY}/${FRONTEND_IMAGE}:${IMAGE_TAG} \
                            --push .
                    """
                }
            }
        }

        stage('Update Chart Version in GitOps Repo') {
            steps {
                script {
                    // Clone the GitOps repo
                    sh '''
                        rm -rf gitops-tmp
                        git clone git@github.com:uio-mana/GitOps-infrastucture.git gitops-tmp
                    '''
                    // Update Chart.yaml in the cloned repo
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
                        baseVersion=$($YQ e '.version' Chart.yaml | sed 's/-rc.*//')
                        newVersion="${baseVersion}-${BRANCH_NAME}.${BUILD_NUMBER}"
                        echo "Setting Chart version to: ${newVersion}"
                        $YQ e -i ".version = \"${newVersion}\"" Chart.yaml
                        $YQ e -i ".appVersion = \"${BUILD_NUMBER}\"" Chart.yaml
                    '''
                    // Commit and push changes
                    sh '''
                        cd gitops-tmp
                        git config user.email "ci-bot@gbif.no"
                        git config user.name "GBIF Jenkins CI"
                        git add apps/publishgpt/Chart.yaml
                        git commit -m "ci: update Chart.yaml version for ${BRANCH_NAME}.${BUILD_NUMBER} [skip ci]" || true
                        git push origin main
                    '''
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