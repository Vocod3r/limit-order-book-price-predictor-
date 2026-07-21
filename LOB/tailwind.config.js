/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        ink: '#0b0f14',
        panel: '#121821',
        'panel-raised': '#171f2a',
        line: '#232b36',
        bid: { DEFAULT: '#3ddc84', dim: '#1f6b45' },
        ask: { DEFAULT: '#ff5c5c', dim: '#7a2c2c' },
        signal: '#f5a623',
        'text-primary': '#e6edf3',
        'text-muted': '#7c8798',
      },
      fontFamily: {
        display: ['"Space Grotesk"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
        ui: ['"Inter"', 'sans-serif'],
      },
    },
  },
  plugins: [],
}