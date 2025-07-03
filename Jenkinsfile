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
    
    stages {
        stage('Check Branch') {
            steps {
                script {
                    // Only proceed for staging and production branches
                    if (env.BRANCH_NAME == 'staging') {
                        echo "🚀 Staging branch detected - proceeding with staging pipeline"
                        env.PIPELINE_TYPE = 'staging'
                    } else if (env.BRANCH_NAME == 'main' || env.BRANCH_NAME == 'master') {
                        echo "🚀 Production branch detected - proceeding with production pipeline"
                        env.PIPELINE_TYPE = 'production'
                    } else {
                        echo "⏭️ Branch '${env.BRANCH_NAME}' is not staging or production - skipping build"
                        currentBuild.result = 'SUCCESS'
                        return
                    }
                }
            }
        }
        
        stage('Load Environment Pipeline') {
            when {
                anyOf {
                    branch 'staging'
                    branch 'main'
                    branch 'master'
                }
            }
            steps {
                script {
                    def jenkinsfilePath
                    
                    if (env.PIPELINE_TYPE == 'staging') {
                        jenkinsfilePath = 'Jenkinsfile.staging'
                        echo "🚀 Loading staging pipeline..."
                    } else if (env.PIPELINE_TYPE == 'production') {
                        jenkinsfilePath = 'Jenkinsfile.production'
                        echo "🚀 Loading production pipeline..."
                    }
                    
                    // Load and execute the appropriate pipeline
                    load jenkinsfilePath
                }
            }
        }
    }
    
    post {
        always {
            script {
                if (env.PIPELINE_TYPE) {
                    echo "✅ Pipeline completed for ${env.PIPELINE_TYPE}"
                } else {
                    echo "⏭️ Build skipped for branch: ${env.BRANCH_NAME}"
                }
            }
        }
    }
} 