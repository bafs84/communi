import ogImage from "../assets/og-image.png";

export const siteConfig = {
  name: "Communi.ch",
  description:
    "Communi.ch ist ein schweizweites Forschungsprojekt zu lokalen Energiegemeinschaften. Gemeinsam untersuchen wir, wie Daten, Koordination und lokale Anreize Stromnetze intelligenter machen.",
  url: "https://communi.ch/",
  lang: "de",
  locale: "de_CH",
  author: "Communi.ch",
  ogImage: ogImage,
  socialLinks: {
    email: "mailto:kontakt@communi.ch",
  },
  navLinks: [
    { text: "Home", href: "/" },
    { text: "ZEV", href: "/zev/" },
    { text: "LEG", href: "/leg/" },
    { text: "Gemeinschaften", href: "/zielgruppen/" },
    { text: "Wissen", href: "/wissen/" },
    { text: "Methoden & Tools", href: "/tools/" },
    { text: "Projekt", href: "/dienstleistungen/" },
    { text: "Mitmachen", href: "/mitgliedschaft/" },
  ],
};
