// This is a Jenkinsfile specifically for production that builds Docker images and updates GitOps configuration.
// The pipeline uses git tags as the source of truth for versioning.
//
// JENKINS CONFIGURATION:
// 1. Create a Pipeline project (not multibranch)
// 2. In Pipeline → Definition → Pipeline script from SCM:
//    - SCM: Git
//    - Repository URL: your-repo-url
//    - Credentials: your-git-credentials
//    - Script Path: Jenkinsfile
// 3. This builds both backend and frontend images, then updates GitOps configuration for production

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

    environment {
        REGISTRY = 'gbifnorway'
        BACKEND_IMAGE = 'chatipt-back-end'
        FRONTEND_IMAGE = 'chatipt-front-end'
        BRANCH_NAME = 'main'
        ENVIRONMENT = 'production'
        NEXT_PUBLIC_BASE_API_URL = 'https://api.chatipt.svc.gbif.no/api'
    }

    stages {
        stage('Get Version from Git') {
            steps {
                script {
                    // Get version from git - more robust approach
                    sh '''
                        echo "Getting version from git..."
                        
                        # Clone the GitOps repo to get the current appVersion
                        rm -rf gitops-tmp
                        git clone https://github.com/gbif-norway/gitops.git gitops-tmp
                        
                        # Read the current appVersion from Chart.yaml
                        cd gitops-tmp/apps/chatipt
                        if ! command -v yq &> /dev/null; then
                            curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o yq
                            chmod +x yq
                            YQ=./yq
                        else
                            YQ=$(command -v yq)
                        fi
                        $YQ --version   
                        APP_VERSION=$($YQ e '.appVersion' Chart.yaml)

                        # Strip off any suffix (anything after the first hyphen)
                        APP_VERSION=$(echo "$APP_VERSION" | cut -d- -f1)
                        
                        echo "Current appVersion from Chart.yaml: $APP_VERSION"
                        
                        # Get short commit SHA
                        cd ../../..
                        COMMIT_SHA=$(git rev-parse --short HEAD)
                        echo "Commit SHA: $COMMIT_SHA"
                        
                        # Get current timestamp for uniqueness
                        TIMESTAMP=$(date +%Y%m%d-%H%M%S)
                        echo "Timestamp: $TIMESTAMP"
                        
                        # Create version string for production using the appVersion from Chart.yaml
                        VERSION="${APP_VERSION}-${COMMIT_SHA}-${TIMESTAMP}"
                        echo "Generated version: $VERSION"
                        
                        # Generate image tag
                        IMAGE_TAG="${VERSION}"
                        echo "Generated image tag: $IMAGE_TAG"
                        
                        # Store version info for later stages
                        echo "$VERSION" > version.txt
                        echo "$IMAGE_TAG" > image_tag.txt
                    '''
                    
                    // Read the version info
                    def version = readFile('version.txt').trim()
                    def imageTag = readFile('image_tag.txt').trim()
                    
                    echo "Version: ${version}"
                    echo "Image tag: ${imageTag}"
                    
                    // Store in environment for post actions
                    env.VERSION = version
                    env.IMAGE_TAG = imageTag
                }
            }
        }

        stage('Build Backend') {
            steps {
                script {
                    def imageTag = readFile('image_tag.txt').trim()
                    echo "Building backend with image tag: ${imageTag}"
                    
                    dir('back-end') {
                        container('kaniko') {
                            sh """
                                /kaniko/executor \\
                                    --context . \\
                                    --dockerfile Dockerfile \\
                                    --destination ${REGISTRY}/${BACKEND_IMAGE}:${imageTag} \\
                                    --cache=true
                            """
                        }
                    }
                }
            }
        }

        stage('Build Frontend') {
            steps {
                script {
                    def imageTag = readFile('image_tag.txt').trim()
                    echo "Building frontend with image tag: ${imageTag}"
                    
                    dir('front-end') {
                        container('kaniko') {
                            sh """
                                /kaniko/executor \\
                                    --context . \\
                                    --dockerfile Dockerfile \\
                                    --destination ${REGISTRY}/${FRONTEND_IMAGE}:${imageTag} \\
                                    --build-arg NEXT_PUBLIC_BASE_API_URL=${NEXT_PUBLIC_BASE_API_URL} \\
                                    --cache=true
                            """
                        }
                    }
                }
            }
        }

        stage('Update GitOps Repo') {
            steps {
                script {
                    def imageTag = readFile('image_tag.txt').trim()
                    echo "Updating GitOps with image tag: ${imageTag}"
                    
                    // Clone the GitOps repo
                    sh '''
                        rm -rf gitops-tmp
                        git clone https://github.com/gbif-norway/gitops.git gitops-tmp
                    '''
                    
                    // Update image tags in values-prod.yaml for production
                    container('kaniko') {
                        withEnv(["IMAGE_TAG=${imageTag}"]) {
                            sh '''
                                cd gitops-tmp/apps/chatipt
                                echo "Updating image tags in values-prod.yaml to $IMAGE_TAG"
                                
                                # Create backup of original file
                                cp values-prod.yaml values-prod.yaml.backup
                                
                                # Update backend image tag
                                sed -i "/^[[:space:]]*backEnd:/,/^[[:space:]]*frontEnd:/ s/^[[:space:]]*tag:[[:space:]]*.*/    tag: $IMAGE_TAG/" values-prod.yaml
                                
                                # Update frontend image tag
                                sed -i "/^[[:space:]]*frontEnd:/,/^[[:space:]]*$/ s/^[[:space:]]*tag:[[:space:]]*.*/    tag: $IMAGE_TAG/" values-prod.yaml
                                
                                # Verify the changes
                                echo "Updated values-prod.yaml:"
                                grep -A 5 -B 5 "tag:" values-prod.yaml
                                
                                echo "Updated image tags successfully"
                            '''
                        }
                    }
                    
                    // Also update Chart.yaml version for production
                    container('kaniko') {
                        withEnv(["IMAGE_TAG=${imageTag}"]) {
                            sh '''
                                cd gitops-tmp/apps/chatipt
                                echo "Updating Chart.yaml version for production release"
                                
                                # Read current version and increment patch
                                currentVersion=$(grep "^version:" Chart.yaml | awk '{print $2}')
                                echo "Current version: $currentVersion"
                                
                                # Increment patch version (e.g., 0.1.0 -> 0.1.1)
                                major=$(echo $currentVersion | cut -d. -f1)
                                minor=$(echo $currentVersion | cut -d. -f2)
                                patch=$(echo $currentVersion | cut -d. -f3)
                                newPatch=$((patch + 1))
                                newVersion="${major}.${minor}.${newPatch}"
                                echo "New version: $newVersion"
                                
                                # Update Chart.yaml
                                sed -i "s/^version: .*/version: $newVersion/" Chart.yaml
                                
                                # Also update appVersion to reflect the new image tag
                                sed -i "s/^appVersion: .*/appVersion: \"$IMAGE_TAG\"/" Chart.yaml
                                
                                echo "Updated Chart.yaml:"
                                grep -E "^(version|appVersion):" Chart.yaml
                            '''
                        }
                    }
                    
                    // Commit and push changes
                    withCredentials([sshUserPrivateKey(credentialsId: 'jenkins-git-ssh', keyFileVariable: 'SSH_KEY', usernameVariable: 'SSH_USER')]) {
                        withEnv(["IMAGE_TAG=${imageTag}"]) {
                            sh '''
                                cd gitops-tmp
                                git config user.email "ci-bot@gbif.no"
                                git config user.name "GBIF Jenkins CI"
                                git add apps/chatipt/values-prod.yaml apps/chatipt/Chart.yaml
                                git commit -m "ci: update production image tags to $IMAGE_TAG and increment chart version [skip ci]" || true
                                git remote set-url origin git@github.com:gbif-norway/gitops.git
                                echo "Pushing to GitHub with SSH..."
                                GIT_SSH_COMMAND="ssh -i $SSH_KEY -o StrictHostKeyChecking=no" git push origin main
                            '''
                        }
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
            echo "🎉 Production pipeline completed successfully"
            echo "📦 Images pushed to registry with tag: ${env.IMAGE_TAG}"
            echo "📋 Based on version: ${env.VERSION}"
        }
        failure {
            echo "❌ Production pipeline failed"
        }
    }
} 