// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract MintBatchA2N8Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 5222163254956769720861560326401075741386994847086210558405366783377756092216;
    uint256 constant alphay  = 19475436559984623554357953129009359053333542731694135281922495325049604221230;
    uint256 constant betax1  = 3059516489220240630186271195914183140946064923114682236776148214202533588863;
    uint256 constant betax2  = 4726638434264925561921847005269901493966648479077249500650785888021982438689;
    uint256 constant betay1  = 20695777602333613504155353915066064094342222466636704397235542841177800495176;
    uint256 constant betay2  = 4930004227668553506141963663146714216566264478789620269782604620259282120034;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 20781152526482178830666062747511220406358255456226069404112887593663304757257;
    uint256 constant deltax2 = 21809892472082075710868649832095618096532505793619463437013850318901007755952;
    uint256 constant deltay1 = 14992341891934999412331569319004605815806558743403501621736612419851830701284;
    uint256 constant deltay2 = 15612862422115297658288643474883760311123045469749367137583518868707990838812;

    
    uint256 constant IC0x = 12552677480065670135176890702069792129008763336371403130082740839184711643234;
    uint256 constant IC0y = 7710613254983581304960444024670656435654399849802662987939430788939347441983;
    
    uint256 constant IC1x = 3232968064178048970064190659875318587968838999602787473077201557266060980631;
    uint256 constant IC1y = 10166073637704252948517083769851001366908453684656105187352058458928521277913;
    
    uint256 constant IC2x = 2199077777321815005188508951116038951171269632368710135628107800710589567758;
    uint256 constant IC2y = 1553075649412429260627044096110325255871940358350760475111796806092641583957;
    
    uint256 constant IC3x = 14512183999482500252495415110537699901789803201550538283560456210695191993606;
    uint256 constant IC3y = 2159542809630604385828638288961726533296440682396516003443318116393038408236;
    
    uint256 constant IC4x = 6914746419371563815833941356160983932541339169116375903550934885238020505971;
    uint256 constant IC4y = 13662293893473866344896026641659405773934382193808927500588889269558307667357;
    
    uint256 constant IC5x = 5485735196158768259762430084468567959617420920492037303068238963559091844089;
    uint256 constant IC5y = 21271437757144123334702794057689605945497363812820812212783784590064069690961;
    
    uint256 constant IC6x = 5415108751841176526351382517088329218242697863850168142524707306173777049623;
    uint256 constant IC6y = 14344885173023987871834127548298172875857084320183287254190155055426406124148;
    
    uint256 constant IC7x = 7670824563796517661629827224107427612167237358940439540167872870463877964265;
    uint256 constant IC7y = 2425794450223777412098521778536731630361028754878745896299269395156500651971;
    
    uint256 constant IC8x = 21040005992244577060833129692814882810017947085316125274629980794283864488691;
    uint256 constant IC8y = 20544213264168234112022088934670923917461143896509951610355730530184935327729;
    
    uint256 constant IC9x = 20645226984574570697978643943460960593898056860789626956055084965169137240106;
    uint256 constant IC9y = 10143923636930124047448708961773766313777211297547782780913917952831963228858;
    
    uint256 constant IC10x = 13795859774202803367838055911589356101925197638636750890389387683678680908152;
    uint256 constant IC10y = 324826609414380189401612216942721204592995700369648095727250131016787749425;
    
    uint256 constant IC11x = 20755734954756770947780003233205066695859950142535399829389379531175624092629;
    uint256 constant IC11y = 18117177000366912806435954997103086113217217649465957466300597838393257344059;
    
    uint256 constant IC12x = 17007102568212232123880852689242206523353458409501283640057929322986832987923;
    uint256 constant IC12y = 7546300288416756912386102451591330363122912489632331853159561249550303251085;
    
    uint256 constant IC13x = 13311570838723847070650933821009752957730859509129573215745788280022907940028;
    uint256 constant IC13y = 14441539238521768379213395333842084752654523312620430999042469233365856146692;
    
    uint256 constant IC14x = 7744770833706853520257237864789000163336679140913606264070221996236944518346;
    uint256 constant IC14y = 16150973666406750531291889414149653818914068837675270580172800942815400501835;
    
    uint256 constant IC15x = 21329433732723210140847410623798893540203037539309873989624527796369417487569;
    uint256 constant IC15y = 10473668646817157555335664879441413030539268571196708001133455351964069873460;
    
    uint256 constant IC16x = 9471949238272968319921735935075719684323604007732444075640044598594234894069;
    uint256 constant IC16y = 343093044180030589623045296499298538197853956741477733904320034750554987856;
    
    uint256 constant IC17x = 19807264661233186105824029816220240088852190067782749719704680639178462758157;
    uint256 constant IC17y = 20875085875320710901293483042552695859250187751846425818420173838833529181825;
    
    uint256 constant IC18x = 19660556395681592327871309376997618823686639716588835265913087960074452729900;
    uint256 constant IC18y = 11582052596487210410958845328317320471072265497367947959817108547230617958119;
    
    uint256 constant IC19x = 9576670318796874190577023386099991952148885990227698736322567690821348644976;
    uint256 constant IC19y = 4119913294276523618954092738378673000264170088452387562214746919124527692641;
    
    uint256 constant IC20x = 11298757455190437237090954125075405319696181815408616217754308254338271885064;
    uint256 constant IC20y = 8833717542795161963107603248000413275909692647377626148957296629794402986676;
    
    uint256 constant IC21x = 18320332993340838288253103110098723469621326545681859553515528853849410886339;
    uint256 constant IC21y = 11349977629415251094050501593833261986025768820603588218125995085117707369894;
    
    uint256 constant IC22x = 19571815818099405540240982199868353748961025707775228805051597548005888528672;
    uint256 constant IC22y = 17266874187270177408838941183425834082261005314409925178528522181196079725227;
    
    uint256 constant IC23x = 8337773510699966075346509322757863010063167946707099760974065872745051056916;
    uint256 constant IC23y = 2287121132069733593529470963781237656901041199755293561863628759194822426934;
    
    uint256 constant IC24x = 3469638558756304013358912883037213625906782436133832859920590143507922409772;
    uint256 constant IC24y = 19630113141143488707395089944090729053240844603105403266741621076058176569674;
    
    uint256 constant IC25x = 6130233343994113777592701977317303605088003216456146001668635336064926232027;
    uint256 constant IC25y = 8083138128291414101767537311746436669794789524166936134270917688094917887672;
    
    uint256 constant IC26x = 5661040073357151138213575830334250477889420517377577099087171192286799032725;
    uint256 constant IC26y = 20600692218730192366657072741060413367406951904183360439857104245406611121946;
    
    uint256 constant IC27x = 14796356068397655370335309503558437280545706967185696066239982225261047633402;
    uint256 constant IC27y = 20866637569871696446115723965444296924071464299905000864583069547819887229506;
    
    uint256 constant IC28x = 9692716525425373766336685685720296181185426020989243559438361318882870053953;
    uint256 constant IC28y = 2877183746740743075865590801915156035447748000132941909505506426305933841230;
    
    uint256 constant IC29x = 17979976146913425072179276621652638915356073367184114039125931186356966533629;
    uint256 constant IC29y = 12526398824922504657052178776574836032463907448755145966508677521958312963806;
    
    uint256 constant IC30x = 6858323046589087853119393241795520830659610697962848162491070773155604793446;
    uint256 constant IC30y = 11004131738134756102829850661380860010833234477768534266906006696333483568425;
    
    uint256 constant IC31x = 12374890733267101009817010058192851591857007685480466541035003963804333590313;
    uint256 constant IC31y = 10884345566965803644613063699885061240736791689230493572094243730629532458112;
    
    uint256 constant IC32x = 14585090568159455732994968827613862431253788811331699646641528511717124887211;
    uint256 constant IC32y = 7176338021690139384306999201337631598907390530830632486712203214764326973190;
    
    uint256 constant IC33x = 7408111241290435609901151417949113050459815632047582385665757270657083313275;
    uint256 constant IC33y = 7252361882317813812397236248622138929206615819373464480254839290553346659443;
    
    uint256 constant IC34x = 21306095560797311410957914565744746022597788732376417867869485473348032179447;
    uint256 constant IC34y = 18975098746961130825785887762404088407107780179954661975103167043442229860695;
    
    uint256 constant IC35x = 13945646610903451487740772999509176987276096359090857920703901025717332136030;
    uint256 constant IC35y = 6671533018501823745329522072980098274158411141352550081587031650285582874375;
    
    uint256 constant IC36x = 13473038661123727608584339101289093065099463657142342968255501793793766420642;
    uint256 constant IC36y = 13750874309087095626684669383651463213226118159070683682583501936230920523410;
    
    uint256 constant IC37x = 3986139651674167069085501514513531719198620830575456532711132040485304226186;
    uint256 constant IC37y = 3281033245317365672912017542639864627465985362892509503415562160102199492564;
    
    uint256 constant IC38x = 13393344927959087737194097736544951541371700991396749625787196727644364642625;
    uint256 constant IC38y = 7526952711536941508035442176445931748390118238398429839619593565952707408739;
    
    uint256 constant IC39x = 15235994778056138413969851755688486603716251402161366135645183207202423535526;
    uint256 constant IC39y = 14034000084400900237172427845440210614704627587956870005738611163654024123684;
    
    uint256 constant IC40x = 14639666593277134610798615257782231371321657836272092559090677205408817963753;
    uint256 constant IC40y = 2485399070484859375658513265944228281897680112126837090031008362244320709022;
    
    uint256 constant IC41x = 18400331514309922681849107628080738960889030204562365119990144014658648527414;
    uint256 constant IC41y = 15166551708525629372357727662093072726920984463216541575136920460655964627417;
    
    uint256 constant IC42x = 3016906356408634452712033737161830262760059776079191718520148469994913527668;
    uint256 constant IC42y = 5819496355983264598936245948894284326635659396711492444116386114617012116839;
    
    uint256 constant IC43x = 12111103480290311337912228000320983588573342939336130081363047758416690547868;
    uint256 constant IC43y = 8124874177631156682078890554268843265731257000751164959605336719929964006051;
    
    uint256 constant IC44x = 11609476741868935215918898328137508407457472141072959570011586766950362752744;
    uint256 constant IC44y = 12645930110975613220653129529177836762729082318738864729498573868391765001365;
    
    uint256 constant IC45x = 15254607897773073735362734944525935900540823422868323438452455329840510975463;
    uint256 constant IC45y = 1628122714578769970628193929983811307007652453990774394456576009706386474800;
    
    uint256 constant IC46x = 5376002186070284894431364732344973733054450872510029721980182185175267376584;
    uint256 constant IC46y = 16006094522586836939840804893438829100157025393879831145321356318958421106474;
    
    uint256 constant IC47x = 8723153577985205525825830313164233367939679828548476809392285607809585738645;
    uint256 constant IC47y = 8995924487773564366662011975783095244782257964296544265823632299955541088403;
    
    uint256 constant IC48x = 19034826999695119084662716416729521961069067768040509131518634702999875929109;
    uint256 constant IC48y = 7791335374765057431941527199524412057096925077519229991668194067338703540328;
    
    uint256 constant IC49x = 11919529255962575048537956206935051795962525619821013231122230855175085041640;
    uint256 constant IC49y = 3545184009691874403096858703117274842409239947659969605593433865491560228257;
    
    uint256 constant IC50x = 5104323793842383934621246947489391403062636571681248842258476091087870112201;
    uint256 constant IC50y = 18292188830685148800143037079192097313938960547496007073556571340405823939096;
    
    uint256 constant IC51x = 21048064894592307756042859062331595882641858797194353509805096041150329441623;
    uint256 constant IC51y = 21879550240773143723886429720985069104868698781170543538386573261195220167939;
    
    uint256 constant IC52x = 15882697784185109058259180133685109905930771186527904665412036049639383104532;
    uint256 constant IC52y = 14018476454215862812132060314556209954583794651926888988041723421437218544295;
    
    uint256 constant IC53x = 7426274879149790045922789466344486066719765862580069562708909693311780794604;
    uint256 constant IC53y = 2572106060953968451439905407697508774897613237902571904078875116935978331554;
    
    uint256 constant IC54x = 1615717460158398306128721788139293640967675105085520340922023987227119714435;
    uint256 constant IC54y = 11954574787772214335266406691814926825291357319269969227371462171071402133401;
    
    uint256 constant IC55x = 11725314433478193007434519568505788720721402000920193440843094974773097417236;
    uint256 constant IC55y = 12418146129744294604517863083357150191424488806219721808641346086349125488969;
    
    uint256 constant IC56x = 15827807650868131233482791776065632725577500635942277392769976335785223934744;
    uint256 constant IC56y = 8955634978919697194531916082966635864323562316416470981928499326638220627443;
    
    uint256 constant IC57x = 6093616046784580987642098103641171390665311653711929233494739017009828387117;
    uint256 constant IC57y = 3219971897483587260773703124374084796772680783699415347563261452596013319276;
    
    uint256 constant IC58x = 16266578868722430646400696385454881854997583999796270900425585866656692352311;
    uint256 constant IC58y = 16493998927927915250659546228784781305893155037326079076242418443337224919437;
    
    uint256 constant IC59x = 6686437201391168253450651085038141745550446639544801797179271056208830812709;
    uint256 constant IC59y = 11044733479256975535183063733488680848000983091985779711320925946014802085398;
    
    uint256 constant IC60x = 14427871090775255219904695924927688179009248888244143720334001421819598716606;
    uint256 constant IC60y = 8298827877730071173705235341507741545181852582519723160383354106941467560269;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[60] calldata _pubSignals) public view returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                
                g1_mulAccC(_pVk, IC43x, IC43y, calldataload(add(pubSignals, 1344)))
                
                g1_mulAccC(_pVk, IC44x, IC44y, calldataload(add(pubSignals, 1376)))
                
                g1_mulAccC(_pVk, IC45x, IC45y, calldataload(add(pubSignals, 1408)))
                
                g1_mulAccC(_pVk, IC46x, IC46y, calldataload(add(pubSignals, 1440)))
                
                g1_mulAccC(_pVk, IC47x, IC47y, calldataload(add(pubSignals, 1472)))
                
                g1_mulAccC(_pVk, IC48x, IC48y, calldataload(add(pubSignals, 1504)))
                
                g1_mulAccC(_pVk, IC49x, IC49y, calldataload(add(pubSignals, 1536)))
                
                g1_mulAccC(_pVk, IC50x, IC50y, calldataload(add(pubSignals, 1568)))
                
                g1_mulAccC(_pVk, IC51x, IC51y, calldataload(add(pubSignals, 1600)))
                
                g1_mulAccC(_pVk, IC52x, IC52y, calldataload(add(pubSignals, 1632)))
                
                g1_mulAccC(_pVk, IC53x, IC53y, calldataload(add(pubSignals, 1664)))
                
                g1_mulAccC(_pVk, IC54x, IC54y, calldataload(add(pubSignals, 1696)))
                
                g1_mulAccC(_pVk, IC55x, IC55y, calldataload(add(pubSignals, 1728)))
                
                g1_mulAccC(_pVk, IC56x, IC56y, calldataload(add(pubSignals, 1760)))
                
                g1_mulAccC(_pVk, IC57x, IC57y, calldataload(add(pubSignals, 1792)))
                
                g1_mulAccC(_pVk, IC58x, IC58y, calldataload(add(pubSignals, 1824)))
                
                g1_mulAccC(_pVk, IC59x, IC59y, calldataload(add(pubSignals, 1856)))
                
                g1_mulAccC(_pVk, IC60x, IC60y, calldataload(add(pubSignals, 1888)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            
            checkField(calldataload(add(_pubSignals, 1344)))
            
            checkField(calldataload(add(_pubSignals, 1376)))
            
            checkField(calldataload(add(_pubSignals, 1408)))
            
            checkField(calldataload(add(_pubSignals, 1440)))
            
            checkField(calldataload(add(_pubSignals, 1472)))
            
            checkField(calldataload(add(_pubSignals, 1504)))
            
            checkField(calldataload(add(_pubSignals, 1536)))
            
            checkField(calldataload(add(_pubSignals, 1568)))
            
            checkField(calldataload(add(_pubSignals, 1600)))
            
            checkField(calldataload(add(_pubSignals, 1632)))
            
            checkField(calldataload(add(_pubSignals, 1664)))
            
            checkField(calldataload(add(_pubSignals, 1696)))
            
            checkField(calldataload(add(_pubSignals, 1728)))
            
            checkField(calldataload(add(_pubSignals, 1760)))
            
            checkField(calldataload(add(_pubSignals, 1792)))
            
            checkField(calldataload(add(_pubSignals, 1824)))
            
            checkField(calldataload(add(_pubSignals, 1856)))
            
            checkField(calldataload(add(_pubSignals, 1888)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
