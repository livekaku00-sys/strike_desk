/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Strike Desk V4.5 terminal palette
        t: {
          bg:      '#0B0F14',
          panel:   '#111821',
          card:    '#151C25',
          border:  '#1E2A3A',
          'border-hi': '#2A3F5A',
          fg:      '#F5F7FA',
          muted:   '#8D98A8',
          dim:     '#4A5568',
        },
        // Legacy desk colors kept for backward compat
        desk: {
          950: '#020617',
          900: '#0f172a',
          800: '#1e293b',
          700: '#334155',
          600: '#475569',
          border: '#1e293b',
          accent: '#f59e0b',
          success: '#10b981',
          danger: '#ef4444',
          warning: '#eab308',
        },
      },
      fontFamily: {
        terminal: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
