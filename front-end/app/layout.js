import './globals.css'
import { Inter, IBM_Plex_Serif } from 'next/font/google'
import 'bootstrap/dist/css/bootstrap.min.css';
import 'bootstrap-icons/font/bootstrap-icons.css';
import { AuthProvider } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import HeaderWrapper, { NavigationProvider } from './components/HeaderWrapper'

const inter = Inter({ subsets: ['latin'] })
const ibmPlexSerif = IBM_Plex_Serif({ subsets: ['latin'], weight: '400' });

export const metadata = {
  title: 'ChatIPT',
  description: 'Publish your spreadsheets to GBIF through a chat interface',
  icons: {
    icon: [
      { url: '/images/chatipt.webp', type: 'image/webp' },
    ],
  },
}

export default function RootLayout({ children }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <link rel="icon" href="/images/chatipt.webp" type="image/webp" />
      </head>
      <body className={inter.className}>
        <ThemeProvider>
          <AuthProvider>
            <NavigationProvider>
              <HeaderWrapper />
              {children}
            </NavigationProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  )
}
