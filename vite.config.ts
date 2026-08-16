import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  envPrefix: ['VITE_', 'TAURI_'],
  build: {
    target: 'es2021',
    minify: !process.env.TAURI_DEBUG ? 'esbuild' : false,
    sourcemap: !!process.env.TAURI_DEBUG,
  },
  test: {
    // Flatpak-builder caches checkout copies under .flatpak-builder/; never run those.
    exclude: [
      '**/node_modules/**',
      '**/dist/**',
      '**/.flatpak-builder/**',
      '**/build-flatpak/**',
      '**/repo-flatpak/**',
    ],
  },
});
