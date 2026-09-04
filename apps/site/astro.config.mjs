import { defineConfig } from "astro/config";
import { unified } from "@astrojs/markdown-remark";
import sitemap from "@astrojs/sitemap";
import tailwindcss from "@tailwindcss/vite";
import icon from "astro-icon";
import remarkReadingTime from "remark-reading-time";

export default defineConfig({
  site: "https://communi.ch/",
  integrations: [sitemap(), icon()],
  markdown: {
    processor: unified({
      remarkPlugins: [
        remarkReadingTime,
        () => {
          return function (tree, file) {
            file.data.astro.frontmatter.minutesRead =
              file.data.readingTime.minutes;
          };
        },
      ],
    }),
  },
  prefetch: {
    prefetchAll: true,
    defaultStrategy: "viewport",
  },
  build: {
    inlineStylesheets: "always",
  },
  vite: {
    plugins: [tailwindcss()],
  },
});
