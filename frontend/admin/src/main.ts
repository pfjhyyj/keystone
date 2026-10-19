import { createApp } from 'vue'
import App from './App.vue'
import 'element-plus/dist/index.css'
import './styles/index.css'
import { makeRouter } from './router'
import { ui } from './plugins/element-plus'

createApp(App).use(ui).use(makeRouter()).mount('#app')
