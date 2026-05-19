import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'happy-dom',
    setupFiles: ['./tests/js/setup.js'],
    include: ['tests/js/**/*.test.js'],
  },
});
