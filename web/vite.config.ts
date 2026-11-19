import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import type { Plugin } from "vite";

const CSP_POLICY = [
  "default-src 'self'",
  // wasm-unsafe-eval keeps the PDF.js OpenJPEG and qcms decoders working;
  // it permits WebAssembly but not JavaScript eval.
  "script-src 'self' 'wasm-unsafe-eval'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  "connect-src 'self'",
  "worker-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "retriv-content-security-policy",
    apply: "build",
    transformIndexHtml(html) {
      return html.replace(
        "<head>",
        `<head>\n    <meta http-equiv="Content-Security-Policy" content="${CSP_POLICY}">`,
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), contentSecurityPolicy()],
  build: {
    modulePreload: {
      // The polyfill is an inline script, which script-src 'self' would
      // block. Every supported browser implements modulepreload.
      polyfill: false,
    },
  },
  server: {
    host: "127.0.0.1",
    port: 9500,
    proxy: {
      "/v1": {
        target: "http://127.0.0.1:9000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
});
