import { defineConfig } from "vite";

// Relative base so the same build works at https://<org>.github.io/<repo>/ and locally.
export default defineConfig({
  base: "./",
  build: { outDir: "dist", sourcemap: false, target: "es2022" },
});
