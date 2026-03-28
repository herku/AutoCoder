FROM node:20-alpine AS builder
RUN npm install -g npm@latest
RUN apk add --no-cache python3 make g++ git
RUN npm install -g @anthropic-ai/claude-code

FROM node:20-alpine
RUN apk update && apk upgrade --no-cache
RUN npm install -g npm@latest
RUN apk add --no-cache git python3 py3-pip bash github-cli

COPY --from=builder /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN ln -sf /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude && chmod +x /usr/local/bin/claude

WORKDIR /workspace
USER node

CMD ["bash"]
