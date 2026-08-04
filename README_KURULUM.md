# Düsseldorf Konsolosluk Botu — GitHub Actions kurulumu

## 1. Yeni GitHub repository oluştur

GitHub'da `KonsoloslukBot` adlı yeni bir repository oluştur. Kodun görünmesini istemiyorsan **Private** seçebilirsin. Ancak özel repository'lerde Actions dakika kotası tüketilir. Çok sık çalıştırma için public repository daha ekonomik olabilir; hiçbir gizli bilgi kaynak koduna yazılmamalıdır.

## 2. Bu klasörün içeriğini repository'ye gönder

Windows terminalinde bu klasöre geçip:

```bat
git init
git add .
git commit -m "Initial konsolosluk bot"
git branch -M main
git remote add origin REPOSITORY_URL_BURAYA
git push -u origin main
```

## 3. GitHub Secrets ekle

Repository > Settings > Secrets and variables > Actions > New repository secret

Aşağıdaki beş secret'ı tek tek ekle:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_TO` → `whatsapp:+4915228460225`
- `TWILIO_WHATSAPP_FROM` → Twilio gönderici numarası
- `TWILIO_CONTENT_SID` → Twilio Content SID

`.env` dosyasını GitHub'a yükleme. `.gitignore` bunu zaten engeller.

## 4. İlk manuel test

Repository > Actions > Düsseldorf Konsolosluk Botu > Run workflow

Çalışma tamamlandığında logda bulunan tarih görünür. Tarih 15.09.2026 veya daha sonraysa WhatsApp gönderilmez.

## 5. Otomatik çalışma

Workflow her 10 dakikada bir tetiklenir. GitHub zamanlanmış workflow'ları yoğunluk nedeniyle bazen gecikebilir.

## Mesaj koşulu

- 14.09.2026 → mesaj
- 15.09.2026 → mesaj yok
- 30.09.2026 → mesaj yok

Aynı erken tarih ikinci kez görülürse `konsolosluk_state.json` nedeniyle tekrar mesaj gönderilmez.
