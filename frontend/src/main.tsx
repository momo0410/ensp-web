import { useEffect, useMemo, useState } from "react"
import { createRoot } from "react-dom/client"
import { MantineProvider, localStorageColorSchemeManager } from "@mantine/core"
import { Notifications } from "@mantine/notifications"
import App from "./App"
import { getSkin, subscribeSkin, buildTheme } from "./skins"
import "@mantine/core/styles.css"
import "@mantine/notifications/styles.css"
import "./styles.css"

// 主题持久化沿用旧版 localStorage key（light/dark/auto 三态）；外观预设独立存 ensp-skin
function Root() {
  const [skin, setSkinState] = useState(getSkin())
  useEffect(() => subscribeSkin(setSkinState), [])
  useEffect(() => {
    document.documentElement.dataset.skin = skin
  }, [skin])
  const theme = useMemo(() => buildTheme(skin), [skin])

  return (
    <MantineProvider
      theme={theme}
      defaultColorScheme="light"
      colorSchemeManager={localStorageColorSchemeManager({ key: "ensp-theme" })}
    >
      <Notifications position="top-right" />
      <App />
    </MantineProvider>
  )
}

createRoot(document.getElementById("root")!).render(<Root />)
