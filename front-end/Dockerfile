FROM node:latest
WORKDIR /app

ARG NEXT_PUBLIC_BASE_API_URL
ENV NEXT_PUBLIC_BASE_API_URL=${NEXT_PUBLIC_BASE_API_URL}

# Cache-busting argument to force rebuild when needed
ARG CACHE_BUST=1
RUN echo "Cache bust: $CACHE_BUST"

COPY package*.json ./
RUN npm install
COPY . ./
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]
# CMD ["tail", "-f", "/dev/null"]

# npm run dev
