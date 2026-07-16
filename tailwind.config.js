/** Build do CSS estatico: npx tailwindcss@3 -c tailwind.config.js -i tailwind.input.css -o web/tailwind.css --minify */
module.exports = {
  content: ['./web/index.html', './web/app.js', './web/proeis-fixes.js'],
  theme: {
    extend: {
      colors: {
        teal: { 400: '#2dd4bf', 500: '#14b8a6', 600: '#0d9488' },
      },
    },
  },
};
