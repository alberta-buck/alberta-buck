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

contract MintBatchN32Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 709997778838307835264383870894323768967370809777328976677908306577656761867;
    uint256 constant alphay  = 12836297161486382489279136251040704944281163072493113933910361141757593083677;
    uint256 constant betax1  = 3684114477087397643340155885713584867253826796058431906225088186474534956028;
    uint256 constant betax2  = 21569416133113965215801657701571922097548973573516190817471638180089358098266;
    uint256 constant betay1  = 4326734521026516125659375800180265709604472291722677762145538662848384222069;
    uint256 constant betay2  = 16828383835347505433443117675638127444543738572256217033768785829213672209398;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 983895279352751390186611505230750603511580408415450241584541093306135245214;
    uint256 constant deltax2 = 13950088539229708351622175637504552355687007666078514295591934446232424394323;
    uint256 constant deltay1 = 19719732635041458344066436222946209803438858380616075150128588017539475466911;
    uint256 constant deltay2 = 8460643479191678914869590370310937624448486014109423611906221295910796541285;

    
    uint256 constant IC0x = 5421733919226487825559244933195292235970427699545064596543562794330307294148;
    uint256 constant IC0y = 7336076838548057676711048584071038924774601331018821168196263394106762850428;
    
    uint256 constant IC1x = 1313501648864056920426824634570898557026150702014726716105546864962057472824;
    uint256 constant IC1y = 5163739850395319544457754946751378563990375143954358471638757804505009785571;
    
    uint256 constant IC2x = 12154433301738248827940669341752069990510285397567442021883775366519069254976;
    uint256 constant IC2y = 20780593943887573413310407274085721952481533892103121514523021756979921956107;
    
    uint256 constant IC3x = 9365155900890519768352712006499047535864872084167281603394462014289789262853;
    uint256 constant IC3y = 8962321101638892356805957331200349402972330486104923931813658855764837961134;
    
    uint256 constant IC4x = 6268857330566240707068854941192347220492498621241017133673264603998358259967;
    uint256 constant IC4y = 10167633602562395258026315632176656151749063547325604442512394098791608084329;
    
    uint256 constant IC5x = 8411799915354119567505998809536020707725890862164430074799969703387839464667;
    uint256 constant IC5y = 12974865746466794930070307552597100018361544321252639040816092052125455367008;
    
    uint256 constant IC6x = 6723745038331477853481917180332028689939150804032306281113813698611312232496;
    uint256 constant IC6y = 8728676738420586349589108392999752501114759998194127225614202161590943127931;
    
    uint256 constant IC7x = 1071898277330573969278262195165372620165354803800774647718593187577431276218;
    uint256 constant IC7y = 18445642593918388602072286602491133833207899784028120814346580655713319280316;
    
    uint256 constant IC8x = 9653778255102492919884920975688125344620838879500091364450308561372932804484;
    uint256 constant IC8y = 8558292271386122271336546250392962155504413712915238930346020731826410484115;
    
    uint256 constant IC9x = 18841320004542023376562263804626966398165543336960551244906271460612908111230;
    uint256 constant IC9y = 3667496930842852340925478689531150112413312079604392159345568069965174901833;
    
    uint256 constant IC10x = 16261978275801501781803879041827441501785402950036570596086445674272422823556;
    uint256 constant IC10y = 6607235972977868943363247790435821968750435455826716986599074171263923646463;
    
    uint256 constant IC11x = 4842108135324523106917473155173000477539981130515098419397714291472497878456;
    uint256 constant IC11y = 243144493207981542164594875608627569207091917529642908916151777201570953834;
    
    uint256 constant IC12x = 20169130212730435180869831225533752872035535559772461167519946643713521256081;
    uint256 constant IC12y = 18129833621682859640236123942277355250431031239960666341100218251296504601738;
    
    uint256 constant IC13x = 17768098104547570853063142161914096169511834896161224804971365534009781119282;
    uint256 constant IC13y = 15463017460090032065066821041714488602731940464663912060208067213442672154695;
    
    uint256 constant IC14x = 547197943912488909981570278181068704100168721378037545971549213979672053628;
    uint256 constant IC14y = 5821621020472173572111905553838394254990806233340133084481063390698006468848;
    
    uint256 constant IC15x = 18019541236549391476660541221781651281046282683811856268675404166331682218091;
    uint256 constant IC15y = 16565588460198809520515657542349167230589139016510359660861979224176903219104;
    
    uint256 constant IC16x = 18785524742148993781988232297181768396285038349765222421810270773009749749912;
    uint256 constant IC16y = 21499928322758397560506987084843780745334961349298813201498869288576161951107;
    
    uint256 constant IC17x = 4369608613351455450210228416923967177360191573125014700210161681495482402060;
    uint256 constant IC17y = 19074031738621384664503342866477296591992562234562685319848865549282803968970;
    
    uint256 constant IC18x = 10403776433899380308638091420305902417119950728297022797970975181137210311009;
    uint256 constant IC18y = 278769441362987728288548679506420598713712195937064025574428869905049661543;
    
    uint256 constant IC19x = 19714964763262126316347665940212834248134613064125558399897772067484699055460;
    uint256 constant IC19y = 17056027209157618349557692118720617127914805228328395406378950727459577333966;
    
    uint256 constant IC20x = 18122956684954348866006149950450295363215764263932364450403953743662716162231;
    uint256 constant IC20y = 10008100908134165977246217269419898652078091827939603055688524286445379917489;
    
    uint256 constant IC21x = 11163201160209857566252471304323120417126604981148883712536942416831188605903;
    uint256 constant IC21y = 20400017557786657480043503139585721949104347468614578896318672260080476990984;
    
    uint256 constant IC22x = 9519595607038091009473079621544230085334576245730720452690102810634409307759;
    uint256 constant IC22y = 19658016733368223413107117947821636958321606898107648918203766664383523673616;
    
    uint256 constant IC23x = 19635243610955916987869837967043212851492427752159158211093583799944528990013;
    uint256 constant IC23y = 3918358630136975818991802687354904386401805776936950851822595494869986855391;
    
    uint256 constant IC24x = 7691515858914583203969415391472900658252119029391385309694996362464552601852;
    uint256 constant IC24y = 11090718384949463575250718438732015679802857585424761553164126197114268894381;
    
    uint256 constant IC25x = 7755081757626746513282700686387791298317184796469750780624248282439991970073;
    uint256 constant IC25y = 4557179413631109774844862942614904504441556687877305388262565548032965205565;
    
    uint256 constant IC26x = 7301620621935560500153151792741332032080223341616097920675823615202901832316;
    uint256 constant IC26y = 8077339313047054558369780034296956514021471680639425727488531519436853152086;
    
    uint256 constant IC27x = 19166239905669749659664711951720847419705876735127129022612662309922877980284;
    uint256 constant IC27y = 10928859353900761666363470690970731439776578175549309707273012585827695160138;
    
    uint256 constant IC28x = 18828520110555703611426853947068855915554133992091953686318871377678373741388;
    uint256 constant IC28y = 13492068747533800644012580018612066555777816632356571164058689450165576005664;
    
    uint256 constant IC29x = 12768041873798729808818458816805737343211406156219851515452996318257921228729;
    uint256 constant IC29y = 13404612697622555126294469296131951734170535532113484274012023422958614347147;
    
    uint256 constant IC30x = 351997513464127481713663241859728584203401653343413540990486231180338726964;
    uint256 constant IC30y = 2857803882975387429197550522447855973999575301352286893462175905238148468702;
    
    uint256 constant IC31x = 10647153761134291557241044031680491571693746037789559093338892331544228025938;
    uint256 constant IC31y = 7558913146485045198866633189700054264411396407399765292177551134762004468462;
    
    uint256 constant IC32x = 19154710663828159661446106783726212924901570035448364023453004072199563690754;
    uint256 constant IC32y = 18841983202478744079756488929148083286595626432278600840812559512051649055152;
    
    uint256 constant IC33x = 20226505184485714662793402530966275652993755532985651273797322172811042151573;
    uint256 constant IC33y = 16853555933530864371712560009805201883440112302991475168806431577107841356279;
    
    uint256 constant IC34x = 7793094546793219304966890752582230565000982726990686036637263568307175710493;
    uint256 constant IC34y = 18407340156198360495756448441151285309427480927968009708628344980957255731579;
    
    uint256 constant IC35x = 17843523769615079799984676618197396683129961062795330694220402689761581031164;
    uint256 constant IC35y = 17597864146192677508287713021058741768425381369638062173780393508811202583053;
    
    uint256 constant IC36x = 13792225547461677113244295196612265550430211499654604684629354606872503230014;
    uint256 constant IC36y = 20047504002070453687875871662759902119733433393727762839857678161469694118981;
    
    uint256 constant IC37x = 1735808877885617839965385264605917133994553624282834891892286490823172183018;
    uint256 constant IC37y = 12232448702433585300550878651477346921308890558785010898743175524705321647644;
    
    uint256 constant IC38x = 11963144746960170342941400959378937149379684653889608601767858271618398767576;
    uint256 constant IC38y = 5259668081799643933416496890374840470894142149220683758543252951663779769732;
    
    uint256 constant IC39x = 15889406166447475860354604608730788100524206138759062675683970006657491885017;
    uint256 constant IC39y = 2118907618763966545944417456091186243797844782947416561455902344691012460108;
    
    uint256 constant IC40x = 386882878048917126901345334665429354482393317335339697908707032187520860765;
    uint256 constant IC40y = 19561606265037938221305964626152888147371337054411131116938453399426694500564;
    
    uint256 constant IC41x = 6253170670468945295375262242934827644617911996084362528534136349343661080140;
    uint256 constant IC41y = 1693520217203160918723190620114069055291854608803429824238474352514252635690;
    
    uint256 constant IC42x = 8896105018321856204204433204692935047163066779732602188830632940777501741215;
    uint256 constant IC42y = 10950957473428269221574443748152265473878079920462215654880744017213871954302;
    
    uint256 constant IC43x = 5207936648046765365445155216618276476591664094530865032470165684424366337280;
    uint256 constant IC43y = 15609307658531763031058320695909065648030740149973307915455162474705328678437;
    
    uint256 constant IC44x = 14422117321911265821981718643223352066494839544682332934562381096599216574852;
    uint256 constant IC44y = 15982110077670984677085899734753090330458524296313286780699228800187855678108;
    
    uint256 constant IC45x = 94131779030904873758789611110151074187629318944176521257347020542571979218;
    uint256 constant IC45y = 7443499485808593800025688230603544751849874473894797894056819212698068983454;
    
    uint256 constant IC46x = 3713415711633237488293442156198630194384581783272586944676395099505770757925;
    uint256 constant IC46y = 1778783570019786673826476756273109346726721930177701738408373946250261027761;
    
    uint256 constant IC47x = 9658861394054006567423609310867308967003224468431187886931591891510869310331;
    uint256 constant IC47y = 16461288000998587317866362626384735413877462461245500605955619328156691831635;
    
    uint256 constant IC48x = 18431815613886939607410374146256582392199298684433367093613476197841860815970;
    uint256 constant IC48y = 19074950346276569078110658168491859161157022444034519668976015951663545337146;
    
    uint256 constant IC49x = 18455975259390417354240614897242022870760786209058420438858083369707739790415;
    uint256 constant IC49y = 16553828541301953888085108506849282710100001732347791034911165970057844397121;
    
    uint256 constant IC50x = 12239854841332346680357147490963543988217845224929136318426739965780585821733;
    uint256 constant IC50y = 7093968132427191004704773098226150175206323219788505155229304665819014702531;
    
    uint256 constant IC51x = 8799431223517630737914139033257498707428670608038394392765708775356297114925;
    uint256 constant IC51y = 15971981699755923076056713167097651278161062076294357900551081877685384839004;
    
    uint256 constant IC52x = 21696357054106645370872163630432888302750898859511549060291916042514307744662;
    uint256 constant IC52y = 19423537885722083426430886477574105796171879118298330099203354817731757599978;
    
    uint256 constant IC53x = 16102899127776942797981722434205629997291954444978400002678644854848316874756;
    uint256 constant IC53y = 21416642761350045210721261306499930525182248120183051131737854085522446945358;
    
    uint256 constant IC54x = 3298081621994317805898253067679481159119692941910652646450878147864394691696;
    uint256 constant IC54y = 10880154283916815832918483039182741472533817435746560057096412188976504230338;
    
    uint256 constant IC55x = 5387100474670473442306645418366629760570504749310297877672466580390680264170;
    uint256 constant IC55y = 8762769323953550368339984030324530703443595389563160051743302774713548055962;
    
    uint256 constant IC56x = 20674392412429045377316837199624045642823671060003950529375475431596515319408;
    uint256 constant IC56y = 10909150857399334174846949596136069748817823764462176087462221277277462486093;
    
    uint256 constant IC57x = 15433146809723933845371464056386633719209196366591424730545786821048402041154;
    uint256 constant IC57y = 7506310049930692604862645104031962531959704718097224340196953402901802274168;
    
    uint256 constant IC58x = 16280376887122215147951985637754291337631555885661670037837903724722619124781;
    uint256 constant IC58y = 4898647361250327761597141623420490318730381505829947167232722322940714352177;
    
    uint256 constant IC59x = 7807186357273182729080591013255645425944457429278180322409935819964101899;
    uint256 constant IC59y = 8417556708274685576862173355307126990185433657516566446423360210867647160406;
    
    uint256 constant IC60x = 16717497746403099495049393963076044891328273821783929412684121849631129049297;
    uint256 constant IC60y = 4974103171296384950437622841377947134974564136456885775265664018044670134555;
    
    uint256 constant IC61x = 16205455427783433696182487691079775007583226910485261145414565587730321500193;
    uint256 constant IC61y = 6382446355058976813915261478904001663557843763488098958256898406679765803884;
    
    uint256 constant IC62x = 6907082084200012965968145047711251663718707111343136031948126856988049968203;
    uint256 constant IC62y = 12500160529476279688223817897400378766717598457945733879846961053288031585629;
    
    uint256 constant IC63x = 6080919915062753844685455097308100671703836151610926368891665815721548861025;
    uint256 constant IC63y = 9573373200605242411722352636349651204079029908597879031279028078198665716109;
    
    uint256 constant IC64x = 11844926978161043888725821775741195982367079468517182340279731374886140763852;
    uint256 constant IC64y = 20415891011357855618278096738192633537051085380168928407137163300451694878434;
    
    uint256 constant IC65x = 11000588258855026461194144046256174102118110983955509909895567415121986444198;
    uint256 constant IC65y = 3632421633427366744106582920280198464345628445801216894360760714164224083641;
    
    uint256 constant IC66x = 1233076445162188478226642602468127350201119556698467256037074170244709070625;
    uint256 constant IC66y = 15066536320791010996182994390184270359475903350346727405676157471628681181214;
    
    uint256 constant IC67x = 7016608292426259718088056134258704770413807067960925088483119485351515845552;
    uint256 constant IC67y = 8741680917209051396005754534575473058671579105192025653071948250289586028292;
    
    uint256 constant IC68x = 6732737107888688063356637334958861007441396861674316616038754416257690233761;
    uint256 constant IC68y = 6874491898680294204768828640899070142914241796192037789839608618922022332991;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[68] calldata _pubSignals) public view returns (bool) {
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
                
                g1_mulAccC(_pVk, IC61x, IC61y, calldataload(add(pubSignals, 1920)))
                
                g1_mulAccC(_pVk, IC62x, IC62y, calldataload(add(pubSignals, 1952)))
                
                g1_mulAccC(_pVk, IC63x, IC63y, calldataload(add(pubSignals, 1984)))
                
                g1_mulAccC(_pVk, IC64x, IC64y, calldataload(add(pubSignals, 2016)))
                
                g1_mulAccC(_pVk, IC65x, IC65y, calldataload(add(pubSignals, 2048)))
                
                g1_mulAccC(_pVk, IC66x, IC66y, calldataload(add(pubSignals, 2080)))
                
                g1_mulAccC(_pVk, IC67x, IC67y, calldataload(add(pubSignals, 2112)))
                
                g1_mulAccC(_pVk, IC68x, IC68y, calldataload(add(pubSignals, 2144)))
                

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
            
            checkField(calldataload(add(_pubSignals, 1920)))
            
            checkField(calldataload(add(_pubSignals, 1952)))
            
            checkField(calldataload(add(_pubSignals, 1984)))
            
            checkField(calldataload(add(_pubSignals, 2016)))
            
            checkField(calldataload(add(_pubSignals, 2048)))
            
            checkField(calldataload(add(_pubSignals, 2080)))
            
            checkField(calldataload(add(_pubSignals, 2112)))
            
            checkField(calldataload(add(_pubSignals, 2144)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
