secret=$(python run_page/get_garmin_secret.py $(sed -n 1p secret.txt) $(sed -n 2p secret.txt) --is-cn)
python run_page/garmin_sync.py $secret --is-cn --only-run
python run_page/tcx_sync.py
python run_page/gen_svg.py --from-db --title "ClF3's Running" --type github --athlete "ClF3" --special-distance 10 --special-distance2 20 --special-color yellow --special-color2 red --output assets/github.svg --use-localtime --min-distance 0.5
python run_page/gen_svg.py --from-db --year $(date +"%Y") --title "$(date +"%Y") Running" --type github --athlete "ClF3" --special-distance 10 --special-distance2 20 --special-color yellow --special-color2 red --output assets/github_$(date +"%Y").svg --use-localtime --min-distance 0.5
python3 run_page/gen_svg.py --from-db --type circular --use-localtime
python run_page/gen_svg.py --from-db --title "Over 10km Runs" --type grid --athlete "ClF3"  --output assets/grid.svg --min-distance 10.0 --special-color yellow --special-color2 red --special-distance 20 --special-distance2 40 --use-localtime
yarn build --outDir /var/www/running --emptyOutDir