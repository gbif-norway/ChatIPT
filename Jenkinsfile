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
    agent any

    environment {
        REGISTRY = 'gbifnorway'
        BACKEND_IMAGE = 'publishgpt-back-end'
        FRONTEND_IMAGE = 'publishgpt-front-end'
        IMAGE_TAG = "${env.BRANCH_NAME}-${env.BUILD_NUMBER}"
        ENVIRONMENT = "${env.BRANCH_NAME}"
    }

    stages {
        stage('Check Branch') {
            when {
                not {
                    anyOf {
                        branch 'staging'
                        branch 'main'
                    }
                }
            }
            steps {
                echo "⏭️ Branch '${env.BRANCH_NAME}' is not staging or main - skipping build"
                script { currentBuild.result = 'SUCCESS' }
            }
        }

        stage('Set Chart Version') {
            when {
                anyOf {
                    branch 'staging'
                    branch 'main'
                }
            }
            steps {
                script {
                    dir('GitOps-infrastucture/apps/publishgpt') {
                        sh '''
                            if ! command -v yq &> /dev/null; then
                                wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
                                chmod +x /usr/local/bin/yq
                            fi
                        '''
                        def baseVersion = sh(script: "yq e '.version' Chart.yaml | sed 's/-rc.*//'", returnStdout: true).trim()
                        def newVersion = "${baseVersion}-${env.BRANCH_NAME}.${env.BUILD_NUMBER}"
                        echo "Setting Chart version to: ${newVersion}"
                        sh "yq e -i '.version = \"${newVersion}\"' Chart.yaml"
                        sh "yq e -i '.appVersion = \"${env.BUILD_NUMBER}\"' Chart.yaml"
                    }
                }
            }
        }

        stage('Setup Docker Buildx') {
            when {
                anyOf {
                    branch 'staging'
                    branch 'main'
                }
            }
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
            when {
                anyOf {
                    branch 'staging'
                    branch 'main'
                }
            }
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
            when {
                anyOf {
                    branch 'staging'
                    branch 'main'
                }
            }
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

        // Add more stages as needed, e.g., update DevOps repo, verify images, etc.
        // Use the same `when` block to restrict to staging/main
    }

    post {
        always {
            cleanWs()
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